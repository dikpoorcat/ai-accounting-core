"""Current membership is complete before accounting amounts or slot plans are read."""

from uuid import uuid4

from test_expense import fact

from ai_accounting_modular.contracts.public import validate_contract
from ai_accounting_modular.domains.operations._description import DescriptionContext
from ai_accounting_modular.domains.operations.public import create_operations


class DirectContext:
    def __init__(self, *facts):
        self.facts = {item["business_id"]: item for item in facts}

    def get_fact(self, business_id):
        return self.facts.get(business_id)

    def __getattr__(self, name):
        raise AssertionError(f"Direct declaration must not read {name}")


def test_declaration_uses_only_facts_and_also_declares_inherited_cooperation_contract():
    source = fact(cooperation_id=str(uuid4()), project_total_fen="200")
    returned = {
        **source,
        "business_id": str(uuid4()),
        "kind": "operations.expense_recovery",
        "data": {
            "expense_id": source["business_id"],
            "counterparty_id": source["data"]["creditor_id"],
            "amount_fen": "50",
            "project_total_fen": "100",
        },
    }
    edges = create_operations().declare_relations(
        [
            {"op": "create", "expected_seq": 0, "fact": returned},
            {"op": "create", "expected_seq": 0, "fact": source},
        ],
        DirectContext(),
    )
    for edge in edges:
        validate_contract("business-facts.schema.json", "Dependency", edge)
    assert {
        edge["consumer_business_id"]
        for edge in edges
        if edge["producer_id"] == source["data"]["cooperation_id"]
    } == {
        source["business_id"],
        returned["business_id"],
    }


def test_replaced_withdrawn_and_new_claims_form_one_final_reverse_membership():
    source = fact()

    def claim():
        return {
            **source,
            "business_id": str(uuid4()),
            "kind": "operations.overpayment",
            "data": {
                "source": {"business_id": source["business_id"], "slot": "primary"},
                "amount_fen": "10",
            },
        }

    old, removed, new = claim(), claim(), claim()

    class Context(DirectContext):
        def related_facts(self, producer_id, relation_kind, **scope):
            return old, removed

    context = DescriptionContext(
        Context(source, old, removed),
        [
            {"op": "recalculate", "expected_seq": 1, "fact": old},
            {"op": "withdraw", "expected_seq": 1, "business_id": removed["business_id"]},
            {"op": "create", "expected_seq": 0, "fact": new},
        ],
    )
    actual = context.related_facts(source["business_id"], "settlement")
    assert {item["business_id"] for item in actual} == {old["business_id"], new["business_id"]}
