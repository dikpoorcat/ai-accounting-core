from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ResourceDefinition


@pytest.fixture
def ids():
    return {
        key: str(uuid4())
        for key in ("business", "source", "party", "bank", "cash", "platform", "expense")
    }


class Context:
    def __init__(self, facts=(), *, objects=(), obligations=()):
        self.facts = {fact["business_id"]: deepcopy(fact) for fact in facts}
        self.objects = {obj["object_id"]: obj for obj in objects}
        self.obligations = {(ob["business_id"], ob["slot"]): ob for ob in obligations}

    def get_fact(self, business_id):
        return deepcopy(self.facts.get(business_id))

    def get_object(self, object_id):
        return deepcopy(self.objects.get(object_id))

    def resolve_sources(self, ids):
        return [{"source_id": value} for value in ids]

    def related_facts(self, producer_id, relation_kind, **scope):
        return tuple(
            deepcopy(fact)
            for fact in self.facts.values()
            if (scope.get("period_from") is None or fact["period"] >= scope["period_from"])
            and (scope.get("period_to") is None or fact["period"] <= scope["period_to"])
        )

    def facts_in_scope(self, kinds, period_from, period_to):
        return tuple(
            deepcopy(fact)
            for fact in self.facts.values()
            if fact["kind"] in kinds and period_from <= fact["period"] <= period_to
        )

    def resource_definitions(self, refs, kind):
        assert kind == "obligation"
        return tuple(
            ResourceDefinition(
                kind,
                deepcopy(ref),
                {
                    "business_id": ref["business_id"],
                    "result_id": ref["business_id"],
                    "result_slot": "main",
                    "result_generation": 1,
                    "content_digest": "0" * 64,
                },
                deepcopy(self.obligations[ref["business_id"], ref["slot"]]),
            )
            for ref in refs
        )


def objects(ids):
    return [
        {"object_id": ids["bank"], "kind": "bank_account"},
        {"object_id": ids["cash"], "kind": "cash_account"},
        {"object_id": ids["platform"], "kind": "platform_account"},
        {"object_id": ids["party"], "kind": "organization"},
    ]


def payable(ids, value="100000", weights=True):
    result = {
        "business_id": ids["expense"],
        "slot": "primary",
        "direction": "payable",
        "category": "trade",
        "amount_fen": value,
        "account_code": "2202",
        "party_id": ids["party"],
        "business_period": "2026-01",
        "allowed_settlements": ["money", "offset", "transfer"],
    }
    if weights:
        result["cashflow_weights"] = [{"category": "other_operating_payments", "weight": "1"}]
    return result


def payment(ids, value="100000", *, account="bank", day="2026-01-20"):
    return {
        "business_id": ids["business"],
        "kind": "money.movement",
        "period": day[:7],
        "effective_on": day,
        "evidence_ids": [ids["source"]],
        "data": {
            "type": "settlement",
            "funds_account_id": ids[account],
            "direction": "outflow",
            "amount_fen": value,
            "payment_method": "individual",
            "counterparty_id": ids["party"],
            "allocations": [
                {"source": {"business_id": ids["expense"], "slot": "primary"}, "amount_fen": value}
            ],
        },
    }
