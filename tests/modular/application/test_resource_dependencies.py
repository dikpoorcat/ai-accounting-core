"""Independent resource dependency checks over the real SQLite application index."""

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

import pytest

from ai_accounting_modular.application._changes import classify
from ai_accounting_modular.application._context import DomainRegistry, SubmissionContext
from ai_accounting_modular.application._planning import collect_changes, evaluate
from ai_accounting_modular.application.public import repository_factory as application_repository
from ai_accounting_modular.contracts.public import (
    ContractError,
    ResourceCutoff,
    ResourceDefinition,
    validate_contract,
)
from ai_accounting_modular.ledger.public import create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.storage.public import Storage

from .test_publications import (
    Candidate as PublicationCandidate,
)
from .test_publications import (
    Ledger as PublicationLedger,
)
from .test_publications import (
    Owner as PublicationOwner,
)
from .test_publications import (
    create,
    direct,
    fact,
    identity,
    posting,
    seed,
)
from .test_publications import system as system


def resource(
    consumer,
    producer=90,
    *,
    mode="use",
    slot="main",
    source_slot="primary",
    kind="obligation",
    start="2026-01",
    end=None,
    on=None,
    excluded=(),
):
    edge = {
        "consumer_business_id": identity(consumer),
        "consumer_result_slot": slot,
        "producer_kind": "business",
        "producer_id": identity(producer),
        "producer_public_slot": source_slot,
        "read_mode": mode,
        "relation_kind": "settlement" if kind == "obligation" else "cost",
        "period_from": start,
        "period_to": end or start,
    }
    if on is not None:
        edge["effective_on"] = on
    if mode == "balance":
        edge["excluded_result_slots"] = [
            {"business_id": identity(number), "result_slot": result_slot}
            for number, result_slot in excluded
        ]
    return validate_contract("business-facts.schema.json", "Dependency", edge)


def balance(consumer, producer=90, **kwargs):
    return resource(consumer, producer, mode="balance", **kwargs)


def observer_ids(repo, uses):
    return tuple(sorted({edge["consumer_business_id"] for edge in repo.resource_observers(uses)}))


class Candidate(PublicationCandidate):
    """Opaque window answers verify scheduling and scope without duplicating balances."""

    def resource_windows(
        self,
        refs,
        kind,
        cutoff,
        *,
        excluding_consumer=None,
        excluding_consumers=(),
        components=None,
    ):
        self.reads.append(
            (
                deepcopy(refs),
                kind,
                cutoff,
                excluding_consumer,
                excluding_consumers,
                set(self.staged),
            )
        )
        return ()

    resource_states = resource_windows

    def resource_definitions(self, refs, kind):
        result = []
        for ref in refs:
            source = {
                "business_id": ref["business_id"],
                "result_slot": "main",
                "result_id": str(uuid5(NAMESPACE_URL, ref["business_id"] + "main")),
                "result_generation": 1,
                "content_digest": "a" * 64,
            }
            value = {
                **ref,
                "amount_fen": "100",
                "business_period": "2026-01",
            }
            if kind == "obligation":
                value.update(
                    direction="payable",
                    category="trade",
                    account_code="2202",
                    allowed_settlements=["money"],
                )
            else:
                value.update(category="money_fund", object_id=identity(999))
            result.append(ResourceDefinition(kind, deepcopy(ref), source, value))
        return tuple(result)


class Ledger(PublicationLedger):
    def begin_candidate(
        self,
        responsibilities,
        unit,
        *,
        publications,
        correction_period,
        resource_uses,
        retained_results,
    ):
        assert retained_results == []
        self.candidate = Candidate(responsibilities, publications)
        self.candidate.resource_uses = deepcopy(resource_uses)
        return self.candidate


class Owner(PublicationOwner):
    def evaluate_slot(self, business_id, slot, context):
        self.evaluated.append((business_id, slot))
        configuration = self.configurations.get(business_id, {})
        for query in configuration.get("windows", {}).get(slot, ()):
            getattr(context, query.get("method", "resource_windows"))(
                query["refs"],
                query.get("kind", "obligation"),
                query["cutoff"],
                excluding_consumer=query.get("single"),
                excluding_consumers=query.get("multiple", ()),
            )
        return posting(context.get_fact(business_id), slot)


def run(system, owner, mutations, *, evaluate_plans=True, ledger=None):
    store, company = system
    ledger, registry = ledger or Ledger(), DomainRegistry([owner])
    with store.write_unit(company) as unit:
        heads = unit.repository("application").heads(owner.facts)
        changes = [
            classify(
                mutation, heads.get(mutation.get("business_id") or mutation["fact"]["business_id"])
            )
            for mutation in mutations
        ]
        context = SubmissionContext(
            unit,
            registry,
            None,
            ledger,
            None,
            facts={change.business_id: change.fact for change in changes},
            evaluation_period_to="2026-12",
        )
        descriptions = collect_changes(changes, (), context, registry, {})
        if evaluate_plans:
            evaluate(descriptions, context, ledger, None)
            return context._candidate
        return descriptions


def query(consumer, producer=90, *, slot="main", cutoff=None, excluded=(), source_slot="primary"):
    return {
        "refs": [{"business_id": identity(producer), "slot": source_slot}],
        "cutoff": cutoff or ResourceCutoff("2026-03"),
        "single": (identity(consumer), slot),
        "multiple": tuple((identity(number), result_slot) for number, result_slot in excluded),
    }


def test_component_notifications_survive_storage_and_ignore_disjoint_uses(system):
    money, offset, full = balance(21), balance(22), balance(23)
    money.pop("excluded_result_slots")
    money["resource_components"] = ["money", "agency"]
    offset["resource_components"] = ["offset"]
    seed(system, [fact(number) for number in range(20, 24)], dependencies=[money, offset, full])
    store, company = system
    with store.read_unit(company) as read:
        repo = read.repository("application")
        assert repo.dependencies([identity(21)]) == (
            {**money, "resource_components": ["agency", "money"]},
        )
        used = resource(20)
        used["resource_components"] = ["money"]
        assert observer_ids(repo, [used]) == (identity(21), identity(23))
        used["resource_components"] = ["offset"]
        assert observer_ids(repo, [used]) == (identity(22), identity(23))
        used.pop("resource_components")
        assert observer_ids(repo, [used]) == (identity(21), identity(22), identity(23))


def test_balance_read_cannot_use_components_outside_its_declared_selection(system):
    watched = balance(61, excluded=((61, "main"),), end="2026-03")
    watched["resource_components"] = ["money"]
    owner = Owner(
        [],
        {
            identity(61): {
                "dependencies": [direct(61, 90), watched],
                "windows": {"main": [query(61)]},
            }
        },
    )
    with pytest.raises(ContractError, match="未声明覆盖"):
        run(system, owner, [create(fact(61)), create(fact(90))])


@pytest.mark.parametrize(
    "reverse,full", [(False, False), (True, False), (False, True), (True, True)]
)
def test_duplicate_balance_declarations_union_components_without_order_dependence(
    system, reverse, full
):
    first = balance(61, excluded=((61, "main"),))
    second = deepcopy(first)
    first["resource_components"] = ["money"]
    if not full:
        second["resource_components"] = ["agency"]
    edges = [first, second]
    if reverse:
        edges.reverse()
    owner = Owner([], {identity(61): {"dependencies": edges}})
    descriptions = run(system, owner, [create(fact(61))], evaluate_plans=False)
    result = descriptions[0][1]["dependencies"][0]
    assert result.get("resource_components") == (None if full else ["agency", "money"])


def test_recalculated_user_notifies_balance_even_when_user_fact_and_edges_are_unchanged(system):
    upstream, user, observer, unrelated, source = (fact(n) for n in (1, 2, 3, 4, 90))
    upstream_edge, used = direct(2, 1), resource(2)
    watched = balance(3, end="2026-03", excluded=((3, "main"),))
    seed(
        system,
        [upstream, user, observer, unrelated, source],
        dependencies=[upstream_edge, used, watched],
    )
    owner = Owner(
        [upstream, user, observer, unrelated, source],
        {
            identity(2): {"dependencies": [upstream_edge, used]},
            identity(3): {"dependencies": [watched]},
        },
    )
    changed = deepcopy(upstream)
    changed["data"]["amount_fen"] = "200"
    run(
        system, owner, [{"op": "replace", "expected_seq": 1, "fact": changed}], evaluate_plans=False
    )
    assert set(owner.described) == {identity(1), identity(2), identity(3)}
    assert identity(4) not in owner.exported and identity(90) not in owner.exported


@pytest.mark.parametrize("operation", ["move", "withdraw"])
def test_old_and_new_use_ranges_notify_observers_and_withdrawal_drops_current_use(
    system, operation
):
    user, first, second = fact(10), fact(11), fact(12)
    old = resource(10)
    first_read = balance(11, end="2026-03")
    second_read = balance(12, start="2026-04", end="2026-06")
    seed(system, [user, first, second], dependencies=[old, first_read, second_read])
    current = resource(10, start="2026-04")
    owner = Owner(
        [user, first, second],
        {
            identity(10): {"dependencies": [current]},
            identity(11): {"dependencies": [first_read]},
            identity(12): {"dependencies": [second_read]},
        },
    )
    changed = deepcopy(user)
    changed["data"]["amount_fen"] = "200"
    mutation = {"op": "replace", "expected_seq": 1, "fact": changed}
    if operation == "withdraw":
        mutation = {"op": "withdraw", "expected_seq": 1, "business_id": identity(10)}
    descriptions = run(system, owner, [mutation], evaluate_plans=False)
    assert identity(11) in owner.described
    assert (identity(12) in owner.described) == (operation == "move")
    actual = [edge for _, description in descriptions for edge in description["dependencies"]]
    assert (any(edge["read_mode"] == "use" for edge in actual)) == (operation == "move")


def test_true_sqlite_notifications_distinguish_slot_kind_day_and_exact_excluded_result(system):
    reads = [
        balance(21, end="2026-03", excluded=((21, "main"),)),
        balance(22, source_slot="vat", end="2026-03"),
        balance(23, kind="cost", end="2026-03"),
        balance(24, on="2026-01-10"),
        balance(25, excluded=((20, "main"),)),
        balance(26, excluded=((20, "other"),)),
    ]
    seed(system, [fact(n) for n in range(20, 27)], dependencies=reads)
    store, company = system
    with store.read_unit(company) as unit:
        repo = unit.repository("application")
        assert observer_ids(repo, [resource(20, on="2026-01-20")]) == (
            identity(21),
            identity(26),
        )
        assert observer_ids(repo, [resource(20)]) == (
            identity(21),
            identity(24),
            identity(26),
        )
        assert observer_ids(repo, [resource(20, source_slot="vat")]) == (identity(22),)
        assert observer_ids(repo, [resource(20, kind="cost")]) == (identity(23),)


def test_nonempty_exclusions_and_day_roundtrip_without_advancing_head_on_identical_write(system):
    original = balance(
        30, end="2026-03", on="2026-03-15", excluded=((30, "netting"), (31, "netting"))
    )
    seed(system, [fact(30)], dependencies=[original])
    store, company = system
    with store.read_unit(company) as unit:
        assert unit.repository("application").dependencies([identity(30)]) == (original,)
        before = unit.head
    with store.write_unit(company) as unit:
        assert (
            unit.repository("application").replace_dependencies(
                [identity(30)], [original], relations=(), search_revision=unit.next_head
            )
            is False
        )
    with store.read_unit(company) as unit:
        assert unit.head == before


def test_related_fact_collection_ignores_resource_metadata_in_storage_and_submission_overlay(
    system,
):
    source, actual, only_use, only_balance = (fact(n) for n in (90, 40, 41, 42))
    true_relation = direct(40, 90)
    true_relation["relation_kind"] = "settlement"
    use, watched = resource(41), balance(42)
    seed(
        system, [source, actual, only_use, only_balance], dependencies=[true_relation, use, watched]
    )
    store, company = system
    owner = Owner([source, actual, only_use, only_balance], {})
    with store.read_unit(company) as unit:
        context = SubmissionContext(
            unit, DomainRegistry([owner]), None, None, None, evaluation_period_to="2026-12"
        )
        assert tuple(
            row["business_id"] for row in context.related_facts(identity(90), "settlement")
        ) == (identity(40),)
        context._dependencies = (true_relation, use, watched)
        for number in (40, 41, 42):
            context.declare_slots(
                {
                    "business_id": identity(number),
                    "op": "recalculate",
                    "complete_result_slots": ["main"],
                }
            )
            context.add_fact(fact(number))
        assert tuple(
            row["business_id"] for row in context.related_facts(identity(90), "settlement")
        ) == (identity(40),)


def test_q4_and_annual_netting_wait_for_true_money_but_not_later_internal_netting(system):
    q4, annual, money, source = (
        fact(51, "2026-12"),
        fact(52, "2026-12"),
        fact(53, "2026-12"),
        fact(90),
    )
    config = {
        identity(51): {
            "slots": ["main", "netting"],
            "dependencies": [
                direct(51, 51, slot="netting"),
                direct(51, 90, slot="netting"),
                balance(
                    51, slot="netting", end="2026-12", excluded=((51, "netting"), (52, "netting"))
                ),
                resource(51, slot="netting", start="2026-12"),
            ],
            "windows": {
                "netting": [
                    query(
                        51,
                        slot="netting",
                        cutoff=ResourceCutoff("2026-12"),
                        excluded=((52, "netting"),),
                    )
                ]
            },
        },
        identity(52): {
            "slots": ["main", "netting"],
            "dependencies": [
                direct(52, 51),
                direct(52, 52, slot="netting"),
                direct(52, 90, slot="netting"),
                balance(52, slot="netting", end="2026-12", excluded=((52, "netting"),)),
                resource(52, slot="netting", start="2026-12"),
            ],
            "windows": {"netting": [query(52, slot="netting", cutoff=ResourceCutoff("2026-12"))]},
        },
        identity(53): {
            "dependencies": [
                direct(53, 51),
                direct(53, 90),
                resource(53, start="2026-12"),
                resource(53, 51, start="2026-12"),
            ],
        },
    }
    owner = Owner([], config)
    completed = run(system, owner, [create(value) for value in (annual, money, source, q4)])
    order = {slot: index for index, slot in enumerate(owner.evaluated)}
    assert order[identity(51), "main"] < order[identity(53), "main"]
    assert order[identity(53), "main"] < order[identity(51), "netting"]
    assert order[identity(51), "netting"] < order[identity(52), "netting"]
    assert completed.completed


@pytest.mark.parametrize(
    "defect", ["missing_definition", "quarter_only", "wrong_exclusion", "other_slot"]
)
def test_partial_scope_requires_definition_full_prior_range_and_exact_exclusions(system, defect):
    consumer, source = fact(61, "2026-06"), fact(90)
    excluded = ((61, "main"),)
    dependency = balance(61, end="2026-06", excluded=excluded)
    dependencies = [direct(61, 90), dependency]
    if defect == "missing_definition":
        dependencies = [dependency]
    elif defect == "quarter_only":
        dependency["period_from"] = "2026-04"
    elif defect == "wrong_exclusion":
        dependency["excluded_result_slots"].append(
            {"business_id": identity(62), "result_slot": "netting"}
        )
    else:
        dependency["consumer_result_slot"] = "other"
    owner = Owner(
        [],
        {
            identity(61): {
                "slots": ["main", "other"],
                "dependencies": dependencies,
                "windows": {"main": [query(61, cutoff=ResourceCutoff("2026-06"))]},
            }
        },
    )
    with pytest.raises(ContractError):
        run(system, owner, [create(consumer), create(source)])


@pytest.mark.parametrize("difference", ["day", "excluded"])
def test_different_balance_semantics_cannot_be_silently_merged(system, difference):
    first = balance(70, end="2026-06", on="2026-06-15", excluded=((70, "main"),))
    second = deepcopy(first)
    if difference == "day":
        second["effective_on"] = "2026-06-16"
    else:
        second["excluded_result_slots"].append({"business_id": identity(71), "result_slot": "main"})
    owner = Owner([], {identity(70): {"dependencies": [first, second]}})
    with pytest.raises(ContractError, match="不能合并不同"):
        run(system, owner, [create(fact(70))], evaluate_plans=False)


def test_explicit_later_day_does_not_block_an_earlier_monthly_window(system):
    consumer, source = fact(80, "2026-06"), fact(90)
    watched = balance(80, end="2026-06", on="2026-06-15", excluded=((80, "main"),))
    owner = Owner(
        [],
        {
            identity(80): {
                "dependencies": [direct(80, 90), watched],
                "windows": {"main": [query(80, cutoff=ResourceCutoff("2026-05"))]},
            }
        },
    )
    result = run(system, owner, [create(consumer), create(source)])
    assert result.completed


@pytest.mark.parametrize("known_later_day", [False, True])
def test_month_only_use_waits_while_source_proven_later_day_does_not(system, known_later_day):
    reader, user, source = fact(81), fact(82), fact(90)
    used = resource(82, on="2026-01-20" if known_later_day else None)
    watched = balance(81, on="2026-01-10", excluded=((81, "main"),))
    owner = Owner(
        [],
        {
            identity(81): {
                "dependencies": [direct(81, 90), watched],
                "windows": {"main": [query(81, cutoff=ResourceCutoff("2026-01", "2026-01-10"))]},
            },
            identity(82): {"dependencies": [direct(82, 90), used]},
        },
    )
    result = run(system, owner, [create(value) for value in (reader, user, source)])
    reader_position = owner.evaluated.index((identity(81), "main"))
    user_position = owner.evaluated.index((identity(82), "main"))
    assert (reader_position < user_position) is known_later_day
    assert result.completed


def test_partial_read_requires_the_actual_resource_producing_slot_not_any_slot_of_business(system):
    class ActualOwner(Owner):
        def evaluate_slot(self, business_id, slot, context):
            plan = super().evaluate_slot(business_id, slot, context)
            # This trusted-program scheduling fixture has no journal to adopt.
            plan["lines"] = []
            if business_id == identity(90) and slot == "main":
                plan["obligations"] = [
                    {
                        "business_id": business_id,
                        "slot": "primary",
                        "direction": "payable",
                        "category": "trade",
                        "amount_fen": "100",
                        "account_code": "2202",
                        "allowed_settlements": ["money"],
                        "business_period": "2026-01",
                    }
                ]
            return plan

        def validate_candidate(self, plan, context):
            assert context._candidate_complete

    store, company = system
    actual = Storage(
        store.catalog_path, repositories=[application_repository(), ledger_repository()]
    )
    owner = ActualOwner(
        [],
        {
            identity(90): {"slots": ["aux", "main"]},
            identity(95): {
                "dependencies": [
                    direct(95, 90, producer_slot="aux"),
                    balance(95, excluded=((95, "main"),)),
                ],
                "windows": {"main": [query(95, cutoff=ResourceCutoff("2026-01"))]},
            },
        },
    )
    with pytest.raises(ContractError, match="生产|定义|来源"):
        run((actual, company), owner, [create(fact(95)), create(fact(90))], ledger=create_ledger())
