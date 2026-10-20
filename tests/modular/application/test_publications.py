"""Application scheduling against strict synthetic owners and real owned indexes.

The candidate returns fixed aggregate answers. These tests verify subscription,
closure and order, without implementing a second tax or reporting calculator.
"""

from copy import deepcopy
from itertools import permutations
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from ai_accounting_modular.application._changes import classify
from ai_accounting_modular.application._context import DomainRegistry, SubmissionContext
from ai_accounting_modular.application._planning import collect_changes, evaluate
from ai_accounting_modular.application.public import repository_factory
from ai_accounting_modular.contracts.public import (
    ContractError,
    PretaxView,
    content_digest,
    validate_contract,
)
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog


def identity(number):
    return f"50000000-0000-4000-8000-{number:012d}"


def fact(number, period="2026-01"):
    value = {
        "business_id": identity(number),
        "kind": "operations.expense",
        "period": period,
        "evidence_ids": [],
        "data": {
            "creditor_id": identity(999),
            "creditor_kind": "supplier",
            "amount_fen": "100",
            "expense_class": "administration",
        },
    }
    validate_contract("business-facts.schema.json", "Fact", value)
    return value


def publication(number, collection, start, end=None, slot="main"):
    return {
        "business_id": identity(number),
        "result_slot": slot,
        "collection": collection,
        "period_from": start,
        "period_to": end or start,
    }


def aggregate(number, collection, start, end=None, slot="main"):
    return {
        "consumer_business_id": identity(number),
        "consumer_result_slot": slot,
        "producer_kind": "aggregate",
        "producer_id": collection,
        "producer_public_slot": "main",
        "read_mode": "result",
        "relation_kind": "calculation",
        "period_from": start,
        "period_to": end or start,
    }


def direct(consumer, producer, *, slot="main", producer_slot="main"):
    return {
        "consumer_business_id": identity(consumer),
        "consumer_result_slot": slot,
        "producer_kind": "business",
        "producer_id": identity(producer),
        "producer_public_slot": producer_slot,
        "read_mode": "result",
        "relation_kind": "calculation",
    }


def posting(current, slot):
    amount, business_id = current["data"]["amount_fen"], current["business_id"]
    plan = {
        "business_id": business_id,
        "result_slot": slot,
        "accounting_period": current["period"],
        "calculation_key": "operations.expense",
        "calculation_revision": 1,
        "rule_refs": [
            {
                "key": "operations.expense",
                "revision": 1,
                "policy_business_ids": [],
                "official_urls": [],
            }
        ],
        "target": {
            "kind": "operations.expense",
            "result_slot": slot,
            "values": {
                "amount_fen": amount,
                "expense_or_income_class": "administration",
                "report_detail_state": "unestablished",
            },
        },
        "basis": {
            "kind": "operations.expense",
            "result_slot": slot,
            "period": current["period"],
            "input": deepcopy(current["data"]),
            "source_results": [],
            "adopted_quantities": [],
            "policies": [],
            "source_locations": [],
        },
        "lines": [
            {
                "line_no": 1,
                "account_code": "5602",
                "debit_fen": amount,
                "credit_fen": "0",
                "summary": "合成应用调度样例",
            },
            {
                "line_no": 2,
                "account_code": "2202",
                "debit_fen": "0",
                "credit_fen": amount,
                "party_id": identity(999),
                "summary": "合成应用调度样例",
            },
        ],
    }
    for key in (
        "obligations",
        "settlements",
        "obligation_reclassifications",
        "cost_lots",
        "cost_consumptions",
        "fund_movements",
        "agency_proofs",
        "tax_sales",
        "report_contributions",
    ):
        plan[key] = []
    return validate_contract("business-facts.schema.json", "PostingPlan", plan)


class Owner:
    supported_kinds = ("operations.expense",)

    def __init__(self, facts, configurations):
        self.facts = {value["business_id"]: value for value in facts}
        self.configurations = configurations
        self.described, self.evaluated, self.exported, self.answers = [], [], [], []

    def export_current(self, ids, read):
        self.exported.extend(ids)
        return tuple(deepcopy(self.facts[key]) for key in ids if key in self.facts)

    def declare_relations(self, changes, context):
        return tuple(
            deepcopy(edge)
            for change in changes
            if change["op"] not in {"withdraw", "handoff_out"}
            for edge in self.configurations.get(change["business_id"], {}).get("dependencies", ())
            if edge["producer_kind"] in {"business", "object"}
            and edge["read_mode"] in {"fact", "result"}
        )

    def describe_change(self, changes, context):
        businesses, dependencies, publications = [], [], []
        for change in changes:
            business_id = change["business_id"]
            self.described.append(business_id)
            configuration = self.configurations.get(business_id, {})
            item = {
                "business_id": business_id,
                "expected_seq": change["expected_seq"],
                "op": change["op"],
                "natural_key": None,
                "complete_result_slots": [],
            }
            if change["op"] not in {"withdraw", "handoff_out"}:
                item.update(
                    fact=deepcopy(change["fact"]),
                    complete_result_slots=configuration.get("slots", ["main"]),
                )
                dependencies.extend(configuration.get("dependencies", ()))
                publications.extend(configuration.get("publications", ()))
            businesses.append(item)
        return {
            "domain": "operations",
            "businesses": businesses,
            "dependencies": deepcopy(dependencies),
            "publications": deepcopy(publications),
            "issues": [],
        }

    def evaluate_slot(self, business_id, slot, context):
        self.evaluated.append((business_id, slot))
        configuration = self.configurations.get(business_id, {})
        for collection, start, end in configuration.get("reads", {}).get(slot, ()):
            answer = getattr(context, collection)(start, end)
            self.answers.append((business_id, slot, answer))
        return posting(context.get_fact(business_id), slot)

    def validate_candidate(self, plan, context):
        assert context._candidate.completed


class Candidate:
    def __init__(self, responsibilities, publications):
        self.expected = {
            (item["business_id"], slot)
            for item in responsibilities
            for slot in item["complete_result_slots"]
        }
        self.publications, self.staged, self.reads = publications, set(), []
        self.completed = False

    def stage(self, plan):
        self.staged.add((plan["business_id"], plan["result_slot"]))

    def complete(self):
        assert self.staged == self.expected
        self.completed = True

    def _read(self, collection, start, end):
        required = {
            (item["business_id"], item["result_slot"])
            for item in self.publications
            if item["collection"] == collection
            and item["period_from"] <= end
            and item["period_to"] >= start
        }
        assert required <= self.staged, "application read aggregate before its publishers"
        self.reads.append((collection, start, end, frozenset(self.staged), self.completed))

    def tax_sales(self, start, end):
        self._read("tax_sales", start, end)
        return ()

    def pre_tax(self, start, end):
        self._read("pre_tax", start, end)
        return PretaxView(
            {
                "period_from": start,
                "period_to": end,
                "operating_revenue": {"state": "known", "amount_fen": "500"},
                "operating_cost": {"state": "known", "amount_fen": "100"},
                "profit_before_tax": {"state": "known", "amount_fen": "400"},
                "basis_digest": "a" * 64,
            }
        )


class Ledger:
    def begin_candidate(
        self, responsibilities, unit, *, publications, correction_period, resource_uses
    ):
        self.candidate = Candidate(responsibilities, publications)
        self.candidate.resource_uses = resource_uses
        return self.candidate

    def prepare_result(self, *args, **kwargs):
        pass

    def get_result_source(self, business_id, slot, unit, *, plan):
        return {
            "business_id": business_id,
            "result_slot": slot,
            "result_id": str(uuid5(NAMESPACE_URL, business_id + slot)),
            "result_generation": 1,
            "content_digest": content_digest(plan),
        }


@pytest.fixture
def system(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成老板", "synthetic-password"))
    store = Storage(catalog, repositories=[repository_factory()])
    company = str(uuid4())
    store.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            "913100000000000001",
            "集合调度合成公司",
            "2026-01",
            tmp_path / "company",
        )
    )
    return store, company


def seed(system, facts, *, dependencies=(), publications=()):
    store, company = system
    with store.write_unit(company) as unit:
        repo = unit.repository("application")
        for current in facts:
            change = classify({"op": "create", "expected_seq": 0, "fact": current}, None)
            repo.save(change.head(str(uuid4()), natural_key=None))
        ids = [current["business_id"] for current in facts]
        repo.replace_dependencies(ids, dependencies)
        repo.replace_publications(ids, publications)


def run(system, owner, mutations):
    store, company = system
    ledger, registry = Ledger(), DomainRegistry([owner])
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
        plans = evaluate(descriptions, context, ledger, None)
        return plans, ledger.candidate


def create(current):
    return {"op": "create", "expected_seq": 0, "fact": current}


def test_new_january_fact_notifies_existing_march_aggregate_without_loading_other_publishers(
    system,
):
    march_consumer, unrelated = fact(1, "2026-03"), fact(3, "2025-01")
    march_edge = aggregate(1, "tax_sales", "2026-03")
    seed(
        system,
        [march_consumer, unrelated],
        dependencies=[march_edge],
        publications=[publication(3, "tax_sales", "2025-01")],
    )
    january = fact(2)
    owner = Owner(
        [march_consumer, unrelated],
        {
            identity(1): {
                "dependencies": [march_edge],
                "reads": {"main": [("tax_sales", "2026-03", "2026-03")]},
            },
            identity(2): {"publications": [publication(2, "tax_sales", "2026-03")]},
        },
    )
    _, candidate = run(system, owner, [create(january)])
    assert owner.evaluated == [(identity(2), "main"), (identity(1), "main")]
    assert identity(3) not in owner.exported and identity(3) not in owner.described
    assert candidate.reads == [
        ("tax_sales", "2026-03", "2026-03", frozenset({(identity(2), "main")}), False)
    ]


@pytest.mark.parametrize("operation", ["move", "withdraw", "retire_slot"])
def test_old_and_new_publication_ranges_notify_consumers_but_retired_slots_are_not_waited_for(
    system, operation
):
    publisher, march, april = fact(4), fact(5, "2026-03"), fact(6, "2026-04")
    old_slot = "extra" if operation == "retire_slot" else "main"
    edges = [aggregate(5, "tax_sales", "2026-03"), aggregate(6, "tax_sales", "2026-04")]
    seed(
        system,
        [publisher, march, april],
        dependencies=edges,
        publications=[publication(4, "tax_sales", "2026-03", slot=old_slot)],
    )
    updated = deepcopy(publisher)
    updated["data"]["amount_fen"] = "200"
    mutations = [{"op": "replace", "expected_seq": 1, "fact": updated}]
    if operation == "withdraw":
        mutations = [{"op": "withdraw", "expected_seq": 1, "business_id": identity(4)}]
    configuration = {
        identity(5): {"dependencies": [edges[0]]},
        identity(6): {"dependencies": [edges[1]]},
    }
    configuration[identity(4)] = (
        {"publications": [publication(4, "tax_sales", "2026-04")]} if operation == "move" else {}
    )
    owner = Owner([publisher, march, april], configuration)
    _, candidate = run(system, owner, mutations)
    assert (identity(5), "main") in owner.evaluated
    assert ((identity(6), "main") in owner.evaluated) == (operation == "move")
    assert (identity(4), "extra") not in candidate.expected
    assert ((identity(4), "main") in candidate.expected) == (operation != "withdraw")


def test_recalculated_publisher_notifies_even_when_fact_dependencies_and_ranges_are_unchanged(
    system,
):
    cost, activation, eit = fact(10), fact(11), fact(12, "2026-03")
    cost_edge = direct(11, 10)
    profit_edge = aggregate(12, "pre_tax", "2026-01", "2026-03")
    pub = publication(11, "pre_tax", "2026-02", "2026-03")
    seed(system, [cost, activation, eit], dependencies=[cost_edge, profit_edge], publications=[pub])
    owner = Owner(
        [cost, activation, eit],
        {
            identity(11): {"dependencies": [cost_edge], "publications": [pub]},
            identity(12): {
                "dependencies": [profit_edge],
                "reads": {"main": [("pre_tax", "2026-01", "2026-03")]},
            },
        },
    )
    changed = deepcopy(cost)
    changed["data"]["amount_fen"] = "200"
    run(system, owner, [{"op": "replace", "expected_seq": 1, "fact": changed}])
    assert owner.evaluated == [
        (identity(10), "main"),
        (identity(11), "main"),
        (identity(12), "main"),
    ]
    assert owner.answers[0][2].fields["profit_before_tax"]["amount_fen"] == "400"


def test_same_collection_newly_discovered_months_are_not_lost_to_a_global_visited_flag(system):
    first, second, third = fact(20), fact(21), fact(22)
    edges = [aggregate(21, "tax_sales", "2026-03"), aggregate(22, "tax_sales", "2026-04")]
    seed(system, [second, third], dependencies=edges)
    owner = Owner(
        [second, third],
        {
            identity(20): {"publications": [publication(20, "tax_sales", "2026-03")]},
            identity(21): {
                "dependencies": [edges[0]],
                "publications": [publication(21, "tax_sales", "2026-04")],
            },
            identity(22): {"dependencies": [edges[1]]},
        },
    )
    run(system, owner, [create(first)])
    assert owner.evaluated == [
        (identity(20), "main"),
        (identity(21), "main"),
        (identity(22), "main"),
    ]


@pytest.mark.parametrize("order", tuple(permutations((31, 32, 33))))
def test_tax_sales_then_pretax_then_eit_uses_slot_graph_before_whole_candidate_completion(
    system, order
):
    # Deliberately sort IDs as EIT, VAT, sale. Scheduler must follow declarations.
    sale, vat, eit = fact(33), fact(32), fact(31)
    owner = Owner(
        [],
        {
            identity(33): {"publications": [publication(33, "tax_sales", "2026-01")]},
            identity(32): {
                "dependencies": [aggregate(32, "tax_sales", "2026-01")],
                "publications": [publication(32, "pre_tax", "2026-01")],
                "reads": {"main": [("tax_sales", "2026-01", "2026-01")]},
            },
            identity(31): {
                "dependencies": [aggregate(31, "pre_tax", "2026-01")],
                "reads": {"main": [("pre_tax", "2026-01", "2026-01")]},
            },
        },
    )
    inputs = {31: eit, 32: vat, 33: sale}
    _, candidate = run(system, owner, [create(inputs[number]) for number in order])
    assert owner.evaluated == [
        (identity(33), "main"),
        (identity(32), "main"),
        (identity(31), "main"),
    ]
    assert all(not read[-1] for read in candidate.reads)
    assert candidate.completed


@pytest.mark.parametrize("declared", [None, ("2026-02", "2026-02")])
def test_aggregate_reads_require_the_active_slot_to_subscribe_to_the_complete_range(
    system, declared
):
    consumer = fact(40)
    configuration = {"reads": {"main": [("pre_tax", "2026-01", "2026-03")]}}
    if declared:
        configuration["dependencies"] = [aggregate(40, "pre_tax", *declared)]
    with pytest.raises(ContractError) as failed:
        run(system, Owner([], {identity(40): configuration}), [create(consumer)])
    assert failed.value.code == "dependency_conflict"


def test_cycle_diagnostic_names_businesses_slots_and_collection_without_evaluating_any_slot(system):
    first, second, independent = fact(51), fact(52), fact(1)
    owner = Owner(
        [],
        {
            identity(51): {
                "dependencies": [aggregate(51, "pre_tax", "2026-01")],
                "publications": [publication(51, "tax_sales", "2026-01")],
            },
            identity(52): {
                "dependencies": [aggregate(52, "tax_sales", "2026-01")],
                "publications": [publication(52, "pre_tax", "2026-01")],
            },
        },
    )
    with pytest.raises(ContractError) as failed:
        run(system, owner, [create(first), create(second), create(independent)])
    assert failed.value.code == "dependency_conflict"
    for text in (identity(51), identity(52), "main", "pre_tax", "tax_sales", "2026-01"):
        assert text in failed.value.message
    assert owner.evaluated == []


def test_another_slot_of_the_same_business_cannot_supply_the_missing_subscription(system):
    owner = Owner(
        [],
        {
            identity(41): {
                "slots": ["main", "other"],
                "dependencies": [aggregate(41, "pre_tax", "2026-01", slot="other")],
                "reads": {"main": [("pre_tax", "2026-01", "2026-01")]},
            }
        },
    )
    with pytest.raises(ContractError) as failed:
        run(system, owner, [create(fact(41))])
    assert failed.value.code == "dependency_conflict"


def test_a_new_consumer_reuses_persisted_publishers_without_exporting_or_recalculating_them(system):
    persisted = fact(43)
    seed(system, [persisted], publications=[publication(43, "pre_tax", "2026-01")])
    owner = Owner(
        [persisted],
        {
            identity(42): {
                "dependencies": [aggregate(42, "pre_tax", "2026-01")],
                "reads": {"main": [("pre_tax", "2026-01", "2026-01")]},
            }
        },
    )
    _, candidate = run(system, owner, [create(fact(42))])
    assert owner.exported == []
    assert owner.evaluated == [(identity(42), "main")]
    # The fixed candidate answer represents the ledger's persisted selection.
    assert candidate.publications == []
    assert owner.answers[0][2].fields["profit_before_tax"]["amount_fen"] == "400"


@pytest.mark.parametrize(
    "malformation", ["foreign_business", "retired_slot", "reversed_range", "missing_publications"]
)
def test_owner_must_explicitly_declare_its_own_valid_current_publications(system, malformation):
    entry = publication(44, "pre_tax", "2026-01")
    owner = Owner([], {identity(44): {"publications": [entry]}})
    if malformation == "foreign_business":
        entry["business_id"] = identity(45)
    elif malformation == "retired_slot":
        entry["result_slot"] = "not_declared"
    elif malformation == "reversed_range":
        entry.update(period_from="2026-03", period_to="2026-01")
    else:
        describe = owner.describe_change

        def missing(changes, context):
            description = describe(changes, context)
            del description["publications"]
            return description

        owner.describe_change = missing
    with pytest.raises(ContractError) as failed:
        run(system, owner, [create(fact(44))])
    assert failed.value.code == "invalid_input"
    assert owner.evaluated == []


def test_publication_index_is_atomic_and_unchanged_projection_does_not_advance_head(system):
    current = fact(60)
    original = publication(60, "pre_tax", "2026-01")
    seed(system, [current], publications=[original])
    store, company = system
    with store.read_unit(company) as unit:
        before = unit.head
    with store.write_unit(company) as unit:
        assert (
            unit.repository("application").replace_publications([identity(60)], [original]) is False
        )
    with store.read_unit(company) as unit:
        assert unit.head == before
    with pytest.raises(RuntimeError):
        with store.write_unit(company) as unit:
            assert (
                unit.repository("application").replace_publications(
                    [identity(60)], [publication(60, "pre_tax", "2026-02")]
                )
                is True
            )
            raise RuntimeError("合成保存失败")
    with store.read_unit(company) as unit:
        assert unit.head == before
        assert unit.repository("application").publications([identity(60)]) == (original,)


def test_publication_projection_replaces_the_complete_business_set_and_preserves_other_businesses(
    system,
):
    first, second = fact(61), fact(62)
    original = [
        publication(61, "tax_sales", "2026-01"),
        publication(61, "pre_tax", "2026-02", slot="extra"),
        publication(62, "pre_tax", "2026-03"),
    ]
    seed(system, [first, second], publications=original)
    store, company = system
    replacement = publication(61, "pre_tax", "2026-04")
    with store.write_unit(company) as unit:
        unit.repository("application").replace_publications([identity(61)], [replacement])
    with store.read_unit(company) as unit:
        assert unit.repository("application").publications([identity(61), identity(62)]) == (
            replacement,
            original[2],
        )
    with store.write_unit(company) as unit:
        unit.repository("application").replace_publications([identity(61)], [])
    with store.read_unit(company) as unit:
        assert unit.repository("application").publications([identity(61), identity(62)]) == (
            original[2],
        )
