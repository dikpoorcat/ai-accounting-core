"""Direct fact membership is independent of owner and submission order.

The narrow owners below deliberately use valid facts as opaque fixtures. They
test application scheduling, not an alternative implementation of accounting.
"""

from itertools import permutations

import pytest

from ai_accounting_modular.application._changes import classify
from ai_accounting_modular.application._context import DomainRegistry, SubmissionContext
from ai_accounting_modular.application._planning import collect_changes, ordered_slots
from ai_accounting_modular.contracts.public import ContractError, validate_contract

from .test_publications import Ledger, create, direct, fact, identity, seed
from .test_publications import Owner as BaseOwner
from .test_publications import system as system


def member(number, parent):
    return validate_contract(
        "business-facts.schema.json",
        "Fact",
        {
            "business_id": identity(number),
            "kind": "operations.deposit",
            "period": "2026-01",
            "evidence_ids": [],
            "data": {"type": "direct", "counterparty_id": identity(parent), "amount_fen": "100"},
        },
    )


def link(current):
    key = (
        current["data"]["creditor_id"]
        if current["kind"] == "operations.expense"
        else current["data"]["counterparty_id"]
    )
    if key == identity(999):
        return []
    return [
        {
            **direct(int(current["business_id"][-12:]), int(key[-12:])),
            "read_mode": "fact",
        }
    ]


class Owner(BaseOwner):
    def __init__(self, kind, facts):
        super().__init__(facts, {})
        self.supported_kinds = (kind,)
        self.seen = {}

    def declare_relations(self, changes, context):
        return tuple(
            edge
            for change in changes
            if change["op"] not in {"withdraw", "handoff_out"}
            for edge in link(change["fact"])
        )

    def describe_change(self, changes, context):
        result = super().describe_change(changes, context)
        for row in result["businesses"]:
            if "fact" not in row:
                continue
            current = row["fact"]
            result["dependencies"].extend(link(current))
            related = context.related_facts(row["business_id"], "calculation")
            self.seen[row["business_id"]] = tuple(item["business_id"] for item in related)
            for other in related:
                result["dependencies"].append(
                    direct(int(row["business_id"][-12:]), int(other["business_id"][-12:]))
                )
            result["dependencies"].append(
                {
                    **direct(int(row["business_id"][-12:]), int(row["business_id"][-12:])),
                    "read_mode": "collection",
                    "period_from": "0001-01",
                    "period_to": "9999-12",
                }
            )
        return result


def run(system, owners, mutations):
    storage, company = system
    ledger, registry = Ledger(), DomainRegistry(owners)
    with storage.write_unit(company) as unit:
        ids = [item.get("business_id") or item["fact"]["business_id"] for item in mutations]
        heads = unit.repository("application").heads(ids)
        changes = [
            classify(item, heads.get(item.get("business_id") or item["fact"]["business_id"]))
            for item in mutations
        ]
        context = SubmissionContext(
            unit,
            registry,
            None,
            ledger,
            None,
            facts={change.business_id: change.fact for change in changes},
            evaluation_period_to="2026-01",
        )
        descriptions = collect_changes(changes, (), context, registry, {})
        return descriptions


@pytest.mark.parametrize("order", tuple(permutations(range(3))))
@pytest.mark.parametrize("owner_order", (False, True))
def test_all_direct_links_exist_before_first_description(system, order, owner_order):
    root, child, leaf = fact(1), member(2, 1), fact(3)
    leaf["data"]["creditor_id"] = child["business_id"]
    owners = [Owner("operations.expense", []), Owner("operations.deposit", [])]
    if owner_order:
        owners.reverse()
    facts = [root, child, leaf]
    descriptions = run(system, owners, [create(facts[index]) for index in order])
    assert next(item for item in owners if "operations.expense" in item.supported_kinds).seen == {
        root["business_id"]: (child["business_id"],),
        leaf["business_id"]: (),
    }
    assert next(item for item in owners if "operations.deposit" in item.supported_kinds).seen == {
        child["business_id"]: (leaf["business_id"],)
    }
    graph = {(current["business_id"], "main"): set() for current in facts}
    for _, description in descriptions:
        for edge in description["dependencies"]:
            if edge["read_mode"] == "result":
                graph[edge["consumer_business_id"], edge["consumer_result_slot"]].add(
                    (edge["producer_id"], edge["producer_public_slot"])
                )
    assert ordered_slots(graph, {}) == tuple(
        (current["business_id"], "main") for current in (leaf, child, root)
    )


@pytest.mark.parametrize("operation", ("replace", "withdraw_and_create"))
def test_old_and_new_membership_notifies_both_readers_without_stale_union(system, operation):
    first, second, before = fact(1), fact(2), member(3, 1)
    roots = Owner("operations.expense", [first, second])
    children = Owner("operations.deposit", [before])
    observers = [
        {
            **direct(number, number),
            "read_mode": "collection",
            "period_from": "0001-01",
            "period_to": "9999-12",
        }
        for number in (1, 2)
    ]
    seed(system, [first, second, before], dependencies=[*observers, *link(before)])
    if operation == "replace":
        after = member(3, 2)
        mutations = [{"op": "replace", "expected_seq": 1, "fact": after}]
    else:
        after = member(4, 2)
        mutations = [
            {"op": "withdraw", "expected_seq": 1, "business_id": before["business_id"]},
            create(after),
        ]
    descriptions = run(system, [roots, children], mutations)
    assert roots.seen == {first["business_id"]: (), second["business_id"]: (after["business_id"],)}
    assert {row["business_id"] for _, result in descriptions for row in result["businesses"]} == {
        first["business_id"],
        second["business_id"],
        before["business_id"],
        after["business_id"],
    }


def test_declared_direct_membership_must_be_present_in_final_description(system, monkeypatch):
    root, child = fact(1), member(2, 1)
    roots, children = Owner("operations.expense", []), Owner("operations.deposit", [])
    describe = children.describe_change

    def incomplete(changes, context):
        result = describe(changes, context)
        result["dependencies"] = [
            edge for edge in result["dependencies"] if edge["read_mode"] != "fact"
        ]
        return result

    monkeypatch.setattr(children, "describe_change", incomplete)
    with pytest.raises(ContractError, match="遗漏已声明的直接关系") as failure:
        run(system, [roots, children], [create(root), create(child)])
    assert failure.value.code == "invalid_input"
    assert (
        child["business_id"] in failure.value.message
        and root["business_id"] in failure.value.message
    )


def test_direct_declaration_cannot_borrow_another_owner_responsibility(system, monkeypatch):
    root, child = fact(1), member(2, 1)
    roots, children = Owner("operations.expense", []), Owner("operations.deposit", [])
    monkeypatch.setattr(roots, "declare_relations", lambda changes, context: tuple(link(child)))
    with pytest.raises(ContractError, match="本领域有效业务"):
        run(system, [roots, children], [create(root), create(child)])


def test_missing_relation_port_is_an_assembly_error_before_any_business_unit():
    class IncompleteOwner:
        supported_kinds = ("operations.expense",)

    with pytest.raises(ContractError, match="declare_relations.*IncompleteOwner") as failure:
        DomainRegistry([IncompleteOwner()])
    assert failure.value.code == "invalid_input"
