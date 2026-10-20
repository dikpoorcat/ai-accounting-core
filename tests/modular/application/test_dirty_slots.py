"""Independent execution oracles: declarations remain complete, plans do not."""

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

import pytest

from ai_accounting_modular.contracts.public import content_digest

from .test_publications import Candidate as BaseCandidate
from .test_publications import Owner as BaseOwner
from .test_publications import aggregate, direct, fact, identity, posting, publication, run, seed
from .test_publications import system as system
from .test_resource_dependencies import balance, resource


def watch(number, *, effect="description", calendar=False):
    edge = {
        **direct(number, number),
        "read_mode": "collection",
        "period_from": "0001-01",
        "period_to": "9999-12",
    }
    if effect is not None:
        edge["notify_effect"] = effect
    if calendar:
        edge.update(producer_kind="calendar", producer_id="work")
    return edge


class Candidate(BaseCandidate):
    def __init__(self, responsibilities, publications, retained):
        super().__init__(responsibilities, publications)
        self.retained = {(ref["business_id"], ref["result_slot"]) for ref in retained}

    def complete(self):
        assert not self.staged & self.retained
        assert self.staged | self.retained == self.expected
        self.completed = True


class Ledger:
    def __init__(self, existing):
        self.plans = {(item["business_id"], item["result_slot"]): item for item in existing}
        self.exact_reads = []
        self.allow_broad = set()
        self.references = {
            key: {
                "business_id": key[0],
                "result_slot": key[1],
                "result_id": str(uuid5(NAMESPACE_URL, key[0] + key[1])),
                "generation": 1,
                "business_period": value["accounting_period"],
                "posting_period": value["accounting_period"],
                "state": "open",
            }
            for key, value in self.plans.items()
        }

    def get_result_refs(self, business_ids, unit):
        return tuple(
            deepcopy(ref) for key, ref in self.references.items() if key[0] in business_ids
        )

    def get_results(self, business_ids, unit):
        if not set(business_ids) <= self.allow_broad:
            raise AssertionError("保留槽应按精确身份读取，不能展开整笔业务。")
        return tuple(
            {"reference": self.references[key], "target": plan["target"], "basis": plan["basis"]}
            for key, plan in self.plans.items()
            if key[0] in business_ids
        )

    def get_result(self, result_id, generation, unit):
        key = next(key for key, ref in self.references.items() if ref["result_id"] == result_id)
        assert generation == 1
        self.exact_reads.append(key)
        return deepcopy(self.plans[key])

    def get_result_source(self, business_id, slot, unit, *, plan=None):
        key = business_id, slot
        return {
            "business_id": business_id,
            "result_slot": slot,
            "result_id": str(uuid5(NAMESPACE_URL, business_id + slot)),
            "result_generation": 1,
            "content_digest": content_digest(plan or self.plans[key]),
        }

    def begin_candidate(self, responsibilities, unit, *, publications, retained_results, **kwargs):
        self.candidate = Candidate(responsibilities, publications, retained_results)
        return self.candidate

    def prepare_result(self, *args, **kwargs):
        pass


class Owner(BaseOwner):
    def evaluate_slot(self, business_id, slot, context):
        if slot == "later":
            previous = context.get_result(business_id, "earlier")
            assert previous.target["values"]["amount_fen"] == "100"
        return super().evaluate_slot(business_id, slot, context)


def test_result_notification_recalculates_only_its_slot_and_reads_retained_predecessor(
    system,
):
    source, consumer, unrelated = fact(1), fact(2), fact(3)
    membership = {**direct(1, 2), "read_mode": "fact"}
    numerical = direct(2, 1, slot="later")
    following = direct(3, 2)
    watcher = watch(2)
    dependencies = [membership, numerical, watcher, following]
    seed(system, [source, consumer, unrelated], dependencies=dependencies)
    configurations = {
        identity(1): {"dependencies": [membership]},
        identity(2): {"slots": ["main", "earlier", "later"], "dependencies": [watcher, numerical]},
        identity(3): {"dependencies": [following]},
    }
    current = [source, consumer, unrelated]
    owner = Owner(current, configurations)
    ledger = Ledger([posting(consumer, slot) for slot in ("main", "earlier", "later")])
    changed = deepcopy(source)
    changed["data"]["amount_fen"] = "200"
    plans, candidate = run(
        system, owner, [{"op": "replace", "expected_seq": 1, "fact": changed}], ledger=ledger
    )
    assert owner.evaluated == [(identity(1), "main"), (identity(2), "later")]
    assert candidate.retained == {(identity(2), "main"), (identity(2), "earlier")}
    assert identity(3) not in owner.exported
    assert ledger.exact_reads == [(identity(2), "earlier")]
    retained = [ref for _, plan in plans for ref in plan["retained_results"]]
    assert retained == [ledger.references[identity(2), slot] for slot in ("earlier", "main")]


def test_calendar_description_discovers_new_slot_without_recalculating_existing_slots(system):
    current = fact(10)
    watcher = watch(10, calendar=True)
    seed(system, [current], dependencies=[watcher])
    owner = BaseOwner(
        [current], {identity(10): {"slots": ["main", "earlier", "new"], "dependencies": [watcher]}}
    )
    ledger = Ledger([posting(current, slot) for slot in ("main", "earlier")])
    _, candidate = run(
        system, owner, [], ledger=ledger, previous_period_to="2026-01", period_to="2026-02"
    )
    assert owner.evaluated == [(identity(10), "new")]
    assert candidate.retained == {(identity(10), "main"), (identity(10), "earlier")}


def test_default_collection_still_invalidates_numeric_consumer(system):
    source, current = fact(20), fact(21)
    membership = {**direct(20, 21), "read_mode": "fact"}
    watcher = watch(21, effect=None)
    seed(system, [source, current], dependencies=[membership, watcher])
    owner = BaseOwner(
        [source, current],
        {identity(20): {"dependencies": [membership]}, identity(21): {"dependencies": [watcher]}},
    )
    ledger = Ledger([posting(current, "main")])
    changed = deepcopy(source)
    changed["data"]["amount_fen"] = "200"
    run(system, owner, [{"op": "replace", "expected_seq": 1, "fact": changed}], ledger=ledger)
    assert owner.evaluated == [(identity(20), "main"), (identity(21), "main")]
    assert not ledger.candidate.retained


def test_removed_publisher_notifies_only_affected_aggregate_slot(system):
    source, current = fact(30), fact(31)
    edge = aggregate(31, "pre_tax", "2026-01", slot="tax")
    published = publication(30, "pre_tax", "2026-01")
    seed(system, [source, current], dependencies=[edge], publications=[published])
    owner = BaseOwner(
        [source, current], {identity(31): {"slots": ["main", "tax"], "dependencies": [edge]}}
    )
    ledger = Ledger([posting(source, "main"), posting(current, "main"), posting(current, "tax")])
    run(
        system,
        owner,
        [{"op": "withdraw", "expected_seq": 1, "business_id": identity(30)}],
        ledger=ledger,
    )
    assert owner.evaluated == [(identity(31), "tax")]
    assert ledger.candidate.retained == {(identity(31), "main")}


def test_changed_publication_discovered_by_structure_recalculates_its_slot(system):
    source, current = fact(40), fact(41)
    membership = {**direct(40, 41), "read_mode": "fact"}
    watcher = watch(41)
    old = publication(41, "pre_tax", "2026-01", slot="tax")
    seed(system, [source, current], dependencies=[membership, watcher], publications=[old])
    owner = BaseOwner(
        [source, current],
        {
            identity(40): {"dependencies": [membership]},
            identity(41): {
                "slots": ["main", "tax"],
                "dependencies": [watcher],
                "publications": [publication(41, "pre_tax", "2026-02", slot="tax")],
            },
        },
    )
    ledger = Ledger([posting(current, slot) for slot in ("main", "tax")])
    changed = deepcopy(source)
    changed["data"]["amount_fen"] = "200"
    run(system, owner, [{"op": "replace", "expected_seq": 1, "fact": changed}], ledger=ledger)
    assert owner.evaluated == [(identity(40), "main"), (identity(41), "tax")]
    assert ledger.candidate.retained == {(identity(41), "main")}


def test_explicit_same_fact_retry_recalculates_all_slots_for_rule_revision_changes(system):
    current = fact(50)
    seed(system, [current])
    owner = BaseOwner([current], {identity(50): {"slots": ["main", "earlier", "later"]}})
    ledger = Ledger([posting(current, slot) for slot in ("main", "earlier", "later")])
    run(system, owner, [{"op": "create", "expected_seq": 0, "fact": current}], ledger=ledger)
    assert set(owner.evaluated) == {(identity(50), slot) for slot in ("main", "earlier", "later")}
    assert not ledger.candidate.retained


@pytest.mark.parametrize("reverse", [False, True])
def test_merging_collection_declarations_preserves_the_stronger_numeric_subscription(
    system, reverse
):
    source, current = fact(60), fact(61)
    membership = {**direct(60, 61), "read_mode": "fact"}
    structural, numeric = watch(61), watch(61, effect=None)
    seed(system, [source, current], dependencies=[membership, structural])
    watches = [structural, numeric]
    owner = BaseOwner(
        [source, current],
        {
            identity(60): {"dependencies": [membership]},
            identity(61): {"dependencies": list(reversed(watches)) if reverse else watches},
        },
    )
    ledger = Ledger([posting(current, "main")])
    changed = deepcopy(source)
    changed["data"]["amount_fen"] = "200"
    plans, _ = run(
        system, owner, [{"op": "replace", "expected_seq": 1, "fact": changed}], ledger=ledger
    )
    assert owner.evaluated == [(identity(60), "main"), (identity(61), "main")]
    edge = next(
        edge
        for _, plan in plans
        for edge in plan["dependencies"]
        if edge["read_mode"] == "collection"
    )
    assert edge["notify_effect"] == "result"


def test_sql_notifications_preserve_distinct_slots_of_one_business(system):
    first, second = balance(71, slot="main"), balance(71, slot="tax")
    first["resource_components"], second["resource_components"] = ["money"], ["offset"]
    structural, numeric = watch(71), {**watch(71, effect=None), "consumer_result_slot": "tax"}
    seed(system, [fact(71)], dependencies=[first, second, structural, numeric])
    storage, company = system
    with storage.read_unit(company) as read:
        repo = read.repository("application")
        use = resource(70)
        use["resource_components"] = ["money"]
        assert [edge["consumer_result_slot"] for edge in repo.resource_observers([use])] == ["main"]
        assert {
            (edge["consumer_result_slot"], edge.get("notify_effect", "result"))
            for edge in repo.observers("business", identity(71), "calculation", {"2026-01"})
        } == {("main", "description"), ("tax", "result")}


def test_derived_result_edges_cannot_feed_back_into_reverse_fact_membership(system):
    source, current, member = fact(80), fact(81), fact(82)
    source_watch, directory_watch = watch(80, effect=None), watch(81)
    old = [direct(81, 80, slot=slot) for slot in ("earlier", "later")]
    membership = {**direct(82, 81), "read_mode": "fact"}
    seed(
        system, [source, current], dependencies=[source_watch, directory_watch, *old], relations=[]
    )

    class DirectOwner(BaseOwner):
        def declare_relations(self, changes, context):
            return tuple(
                membership for change in changes if change["business_id"] == member["business_id"]
            )

    owner = DirectOwner(
        [source, current],
        {
            identity(80): {"dependencies": [source_watch]},
            identity(81): {
                "slots": ["main", "earlier", "later"],
                "dependencies": [directory_watch, *old, direct(81, 82, slot="later")],
            },
            identity(82): {"dependencies": [membership]},
        },
    )
    ledger = Ledger(
        [
            posting(source, "main"),
            *(posting(current, slot) for slot in ("main", "earlier", "later")),
        ]
    )
    ledger.allow_broad.add(identity(80))
    run(system, owner, [{"op": "create", "expected_seq": 0, "fact": member}], ledger=ledger)
    assert owner.evaluated == [(identity(82), "main"), (identity(81), "later")]
    assert identity(80) not in owner.described
    assert ledger.candidate.retained == {(identity(81), "main"), (identity(81), "earlier")}
