"""A synthetic zero-result owner uses real identity, records and formal storage.

The existing expense schema is an opaque fixture here. This verifies the owner
protocol without adding a production management business or alternative ledger.
"""

from copy import deepcopy
from uuid import uuid4

from ai_accounting_modular.application.public import create_application, create_backup_verifier
from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.domains.operations.public import Operations
from ai_accounting_modular.records.public import open_standard_bundle

from .conftest import get, initial_request


class ManagedFactOwner(Operations):
    def describe_change(self, changes, context):
        description = super().describe_change(changes, context)
        for responsibility in description["businesses"]:
            responsibility["complete_result_slots"] = []
        description.update(dependencies=[], publications=[])
        return description

    def evaluate_slot(self, *args):
        raise AssertionError("A fact with no result responsibility must never be evaluated")

    def validate_candidate(self, plan, context):
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        assert plan["plans"] == plan["retained_results"] == []


def assemble(base):
    owner = ManagedFactOwner()
    app = create_application(
        storage=base.storage,
        identity=base.identity,
        ledger=base.ledger,
        records=base.records,
        domains=[owner],
    )
    verifier = create_backup_verifier(
        identity=app.identity,
        ledger=app.ledger,
        records=app.records,
        domains=[owner],
        repositories=(),
    )
    return app, owner, verifier


def find(app, company, object_id):
    return app.accounting_find(
        {
            "company_id": company,
            "family": "operations",
            "period_from": "2026-01",
            "period_to": "2026-12",
            "object_id": object_id,
            "limit": 1,
        }
    )


def test_zero_result_fact_is_sourced_findable_exportable_and_idempotent(assembled):
    base, company = assembled
    app, owner, verifier = assemble(base)
    request = initial_request(company)
    business = request["mutations"][0]["fact"]
    party = request["objects"][0]["object_id"]
    saved = app.accounting_apply(request)
    assert saved["changes"] == "committed", saved
    assert saved["accounting"] == "unchanged"
    assert saved["businesses"][0]["results"] == []
    assert find(app, company, party)["items"][0]["business_id"] == business["business_id"]
    with app.storage.read_unit(company) as read:
        before = read.head
        repo = read.repository("application")
        assert repo.dependencies([business["business_id"]]) == ()
        assert repo.relations([business["business_id"]]) == (
            {
                "consumer_business_id": business["business_id"],
                "producer_kind": "object",
                "producer_id": party,
                "relation_kind": "identity",
            },
        )
        assert app.ledger.get_result_refs([business["business_id"]], read) == ()
        assert verifier.verify_snapshot(read).document_set_id
        root = app.storage.company_metadata(read).company_root
    bundle = open_standard_bundle(root / "standard/current.json")
    assert bundle.facts == (business,)
    assert {item["object_id"] for item in bundle.objects} == {party}
    assert bundle.sources == tuple(request["sources"])
    request["command_id"] = str(uuid4())
    repeated = app.accounting_apply(request)
    assert repeated["changes"] == "none", repeated
    with app.storage.read_unit(company) as read:
        assert read.head == before


def test_relation_replacement_and_withdrawal_share_the_fact_transaction(assembled, monkeypatch):
    base, company = assembled
    app, owner, verifier = assemble(base)
    request = initial_request(company)
    business = request["mutations"][0]["fact"]
    old_party = business["data"]["creditor_id"]
    assert app.accounting_apply(request)["completion"] == "complete"
    new_party = str(uuid4())
    final = deepcopy(business)
    final["data"]["creditor_id"] = new_party
    change = {
        "company_id": company,
        "contract": request["contract"],
        "command_id": str(uuid4()),
        "mutations": [{"op": "replace", "expected_seq": 1, "fact": final}],
        "objects": [{**request["objects"][0], "object_id": new_party}],
    }
    original = owner.apply_current

    def fail_at_commit(plan, unit):
        original(plan, unit)

        def fail():
            raise ContractError("dependency_conflict", "Synthetic final atomicity check")

        unit.check_before_commit(fail)

    with monkeypatch.context() as patch:
        patch.setattr(owner, "apply_current", fail_at_commit)
        refused = app.accounting_apply(change)
    assert refused["completion"] == "not_saved" and refused["error"] == "dependency_conflict", (
        refused
    )
    assert get(app, company, business["business_id"])["businesses"][0]["fact"] == business
    assert find(app, company, old_party)["items"]
    assert not find(app, company, new_party)["items"]
    saved = app.accounting_apply(change)
    assert saved["completion"] == "complete", saved
    assert not find(app, company, old_party)["items"]
    assert find(app, company, new_party)["items"]
    withdrawn = app.accounting_apply(
        {
            "company_id": company,
            "contract": request["contract"],
            "command_id": str(uuid4()),
            "mutations": [
                {"op": "withdraw", "expected_seq": 2, "business_id": business["business_id"]}
            ],
        }
    )
    assert withdrawn["completion"] == "complete", withdrawn
    assert not find(app, company, new_party)["items"]
    with app.storage.read_unit(company) as read:
        assert read.repository("application").relations([business["business_id"]]) == ()
        verifier.verify_snapshot(read)
