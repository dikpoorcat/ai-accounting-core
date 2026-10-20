"""Real owners exercise final identities, dependency sets and persistent work scope."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from tests.modular.domains.assets.test_application import assembled as _asset_fixture
from tests.modular.domains.assets.test_application import request as asset_request
from tests.modular.domains.assets.test_application import result_by_slot
from tests.modular.domains.assets.test_assets import case as asset_case
from tests.modular.domains.people.test_application import assembled as _people_fixture
from tests.modular.domains.people.test_application import request as people_request
from tests.modular.domains.people.test_people import case as people_case
from tests.modular.domains.people.test_people import no_contributions
from tests.modular.money.test_application import assemble as assemble_money
from tests.modular.money.test_application import get, request

from .conftest import initial_request, source

asset_system = _asset_fixture
people_system = _people_fixture


def bank_account(object_id):
    return {
        "object_id": object_id,
        "kind": "bank_account",
        "name": "合成银行",
        "expected_seq": 0,
        "data": {
            "bank_name": "合成银行",
            "account_number": object_id,
            "currency": "CNY",
            "owner": "company",
            "opened_period": "2026-01",
            "zero_at_opening": True,
        },
    }


def empty_statement(account_id):
    statement = {
        "business_id": str(uuid4()),
        "kind": "money.bank_statement",
        "period": "2026-01",
        "evidence_ids": [],
        "data": {
            "funds_account_id": account_id,
            "opening_fen": "0",
            "closing_fen": "0",
            "rows": [],
        },
    }
    reconciliation = {
        "business_id": str(uuid4()),
        "kind": "money.reconciliation",
        "period": "2026-01",
        "evidence_ids": [],
        "data": {"statement_id": statement["business_id"], "matches": []},
    }
    return statement, reconciliation


def document_set(app, company):
    status = app.accounting_documents({"company_id": company, "action": "export"})
    assert status["state"] == "ready", status
    path = Path(status["manifest_path"])
    return path.parent.parent, json.loads(path.read_text(encoding="utf-8"))


def test_recalculated_natural_key_follows_statement_account_without_changing_fact_seq(tmp_path):
    app, storage, company, _, _ = assemble_money(tmp_path)
    first_account, second_account = str(uuid4()), str(uuid4())
    statement, reconciliation = empty_statement(first_account)
    created = app.accounting_apply(
        request(
            company,
            reconciliation,
            statement,
            objects=[bank_account(first_account), bank_account(second_account)],
        )
    )
    assert created["completion"] == "complete", created
    statement["data"]["funds_account_id"] = second_account
    changed = app.accounting_apply(
        request(company, mutations=[{"op": "replace", "expected_seq": 1, "fact": statement}])
    )
    assert changed["completion"] == "complete", changed
    current = get(app, company, reconciliation["business_id"])["businesses"][0]
    assert current["fact"] == reconciliation
    assert current["state"]["seq"] == 1
    with storage.read_unit(company) as read:
        projected = read.repository("application").heads([reconciliation["business_id"]])
    assert projected[reconciliation["business_id"]]["natural_key"] == second_account + "/2026-01"

    before = get(app, company, statement["business_id"], reconciliation["business_id"])
    duplicate = {**reconciliation, "business_id": str(uuid4())}
    rejected = app.accounting_apply(request(company, duplicate))
    assert rejected["completion"] == "not_saved" and rejected["error"] == "conflict", rejected
    assert get(app, company, statement["business_id"], reconciliation["business_id"]) == before
    assert get(app, company, duplicate["business_id"])["businesses"] == []

    # Moving to B releases A's identity, rather than leaving a stale reservation.
    replacement_statement, replacement_check = empty_statement(first_account)
    reused = app.accounting_apply(request(company, replacement_check, replacement_statement))
    assert reused["completion"] == "complete", reused


def test_wage_natural_keys_can_swap_in_one_unit_and_retry_preserves_company_head(people_system):
    app, company = people_system
    january = people_case("B17Z")
    february = deepcopy(january)
    february.update(business_id=str(uuid4()), period="2026-02")
    february["data"]["tax_basis"]["tax_period"] = "2026-02"
    created = app.accounting_apply(people_request(company, no_contributions(), january, february))
    assert created["completion"] == "complete", created
    january["period"], february["period"] = "2026-02", "2026-01"
    for fact in (january, february):
        fact["data"]["tax_basis"]["tax_period"] = fact["period"]
    swap = request(
        company,
        mutations=[
            {"op": "replace", "expected_seq": 1, "fact": fact} for fact in (january, february)
        ],
    )
    changed = app.accounting_apply(swap)
    assert changed["completion"] == "complete", changed
    current = get(app, company, january["business_id"], february["business_id"])
    assert [item["fact"]["period"] for item in current["businesses"]] == ["2026-02", "2026-01"]
    assert [item["state"]["seq"] for item in current["businesses"]] == [2, 2]
    retry = app.accounting_apply({**swap, "command_id": str(uuid4())})
    assert retry["changes"] == "none" and retry["accounting"] == "unchanged", retry
    assert get(app, company, january["business_id"], february["business_id"]) == current


def test_two_resource_slots_from_one_business_form_one_dependency_set(asset_system):
    app, company = asset_system
    batch, payer = asset_case("B20R"), str(uuid4())
    reimbursement = {
        "business_id": str(uuid4()),
        "kind": "operations.reimbursement",
        "period": "2026-01",
        "effective_on": "2026-01-20",
        "evidence_ids": batch["evidence_ids"],
        "data": {
            "type": "personal_payment",
            "payer_id": payer,
            "payer_kind": "owner",
            "amount_fen": "30000",
            "allocations": [
                {
                    "source": {
                        "business_id": batch["business_id"],
                        "slot": "reimbursement:" + creditor["person_id"],
                    },
                    "recipient_id": creditor["person_id"],
                    "amount_fen": creditor["amount_fen"],
                }
                for creditor in batch["data"]["creditors"]
            ],
        },
    }
    call = asset_request(company, batch)
    call["objects"].append(
        {
            "object_id": payer,
            "kind": "person",
            "name": "合成付款老板",
            "expected_seq": 0,
            "data": {},
        }
    )
    call["mutations"].insert(0, {"op": "create", "expected_seq": 0, "fact": reimbursement})
    created = app.accounting_apply(call)
    assert created["completion"] == "complete", created
    current = get(app, company, batch["business_id"], reimbursement["business_id"])
    result = current["businesses"][1]["results"][0]
    assert result["target"]["values"]["amount_fen"] == "30000"
    with app.storage.read_unit(company) as read:
        balances = app.ledger.resolve(
            [item["source"] for item in reimbursement["data"]["allocations"]],
            "obligation",
            read,
        )
    assert [item["balance"]["remaining_fen"] for item in balances] == ["0", "0"]
    assert [item["balance"]["transferred_out_fen"] for item in balances] == ["12000", "18000"]
    retried = app.accounting_apply({**call, "command_id": str(uuid4())})
    assert retried["changes"] == "none" and retried["accounting"] == "unchanged", retried
    assert get(app, company, batch["business_id"], reimbursement["business_id"]) == current


def test_unchanged_asset_retry_does_not_advance_company_head_or_formal_generation(asset_system):
    app, company = asset_system
    acquired, enabled = asset_case("B20"), asset_case("B21")
    call = asset_request(company, acquired, enabled)
    assert app.accounting_apply(call)["completion"] == "complete"
    before = get(app, company, acquired["business_id"], enabled["business_id"])
    retry = app.accounting_apply({**call, "command_id": str(uuid4())})
    assert retry["changes"] == "none" and retry["accounting"] == "unchanged", retry
    assert get(app, company, acquired["business_id"], enabled["business_id"]) == before


def test_withdrawing_only_march_fact_does_not_forget_reached_asset_work_month(asset_system):
    app, company = asset_system
    future = asset_case("B18")
    future["period"] = "2026-03"
    assert app.accounting_apply(asset_request(company, future))["completion"] == "complete"
    withdrawn = app.accounting_apply(
        request(
            company,
            mutations=[{"op": "withdraw", "expected_seq": 1, "business_id": future["business_id"]}],
        )
    )
    assert withdrawn["completion"] == "complete", withdrawn
    _, manifest = document_set(app, company)
    assert manifest["company_identity"]["working_period_to"] == "2026-03"
    acquired, enabled = asset_case("B20"), asset_case("B21")
    created = app.accounting_apply(asset_request(company, acquired, enabled))
    assert created["completion"] == "complete", created
    results = result_by_slot(app, company, enabled["business_id"])
    assert set(results) == {"main", "depreciation:2026-02", "depreciation:2026-03"}
    assert results["depreciation:2026-02"]["target"]["values"]["total_amount_fen"] == "3333"
    assert results["depreciation:2026-03"]["target"]["values"]["total_amount_fen"] == "3333"


def test_same_fact_recalculates_after_rule_upgrade_then_retries_without_writing(
    people_system, monkeypatch
):
    app, company = people_system
    wage = people_case("B17Z")
    assert (
        app.accounting_apply(people_request(company, no_contributions(), wage))["completion"]
        == "complete"
    )
    before = get(app, company, wage["business_id"])
    owner = app.registry.owner(wage["kind"])
    evaluate = owner.evaluate_slot
    evaluated = []

    def upgraded(business_id, slot, context):
        evaluated.append((business_id, slot))
        plan = evaluate(business_id, slot, context)
        plan["calculation_revision"] = 2
        plan["rule_refs"][0]["revision"] = 2
        return plan

    monkeypatch.setattr(owner, "evaluate_slot", upgraded)
    replay = request(company, wage)
    adopted = app.accounting_apply(replay)
    assert adopted["changes"] == "committed" and adopted["accounting"] == "saved", adopted
    after = get(app, company, wage["business_id"])
    assert after["head"] == before["head"] + 1
    assert after["businesses"][0]["fact"] == wage
    assert after["businesses"][0]["state"]["seq"] == 1
    assert after["businesses"][0]["results"][0]["reference"]["generation"] == 2
    retry = app.accounting_apply(replay)
    assert retry["changes"] == "none" and retry["accounting"] == "unchanged", retry
    assert get(app, company, wage["business_id"]) == after
    assert evaluated == [(wage["business_id"], "main")] * 2


def test_final_related_and_scoped_facts_overlay_replacements_withdrawals_and_new_members(
    assembled, monkeypatch
):
    app, company = assembled
    call = initial_request(company)
    first = call["mutations"][0]["fact"]
    removed = {**deepcopy(first), "business_id": str(uuid4())}
    call["mutations"].append({"op": "create", "expected_seq": 0, "fact": removed})
    assert app.accounting_apply(call)["completion"] == "complete"
    old_party, new_party = first["data"]["creditor_id"], str(uuid4())
    changed, added = deepcopy(first), {**deepcopy(first), "business_id": str(uuid4())}
    changed["data"]["creditor_id"] = new_party
    owner = app.registry.owner(first["kind"])
    validate = owner.validate_candidate
    observed = []

    def inspect(plan, context):
        observed.append(
            (
                context.related_facts(old_party, "identity", producer_kind="object"),
                context.related_facts(new_party, "identity", producer_kind="object"),
                context.facts_in_scope((first["kind"],), "2026-01", "2026-01"),
                context.related_facts_many(
                    (old_party, new_party, old_party), "identity", producer_kind="object"
                ),
            )
        )
        return validate(plan, context)

    monkeypatch.setattr(owner, "validate_candidate", inspect)
    result = app.accounting_apply(
        request(
            company,
            objects=[
                {
                    "object_id": new_party,
                    "kind": "organization",
                    "name": "合成新供应商",
                    "expected_seq": 0,
                    "data": {},
                }
            ],
            mutations=[
                {"op": "withdraw", "business_id": removed["business_id"], "expected_seq": 1},
                {"op": "create", "expected_seq": 0, "fact": added},
                {"op": "replace", "expected_seq": 1, "fact": changed},
            ],
        )
    )
    assert result["completion"] == "complete", result
    assert len(observed) == 1
    old_group, new_group, scope, combined = observed[0]
    assert old_group == (added,) and new_group == (changed,)
    assert {fact["business_id"] for fact in scope} == {first["business_id"], added["business_id"]}
    assert {fact["business_id"] for fact in combined} == {
        first["business_id"],
        added["business_id"],
    }


def test_nested_source_missing_pointer_rolls_back_then_get_and_documents_keep_source(tmp_path):
    app, storage, company, _, _ = assemble_money(tmp_path)
    account, evidence = bank_account(str(uuid4())), source("合成银行原件：实际入款一分。")
    statement, _ = empty_statement(account["object_id"])
    statement["data"].update(
        closing_fen="1",
        rows=[
            {
                "row_id": str(uuid4()),
                "source": {"source_id": evidence["source_id"], "location": "原件第一行"},
                "actual_on": "2026-01-02",
                "direction": "inflow",
                "amount_fen": "1",
            }
        ],
    )
    rejected = app.accounting_apply(request(company, statement, objects=[account]))
    assert rejected["completion"] == "not_saved" and rejected["error"] == "needs_information"
    assert rejected["fact_issues"][0]["field"] == "/mutations/0/fact/data/rows/0/source/source_id"
    assert rejected["fact_issues"][0]["business_id"] == statement["business_id"]
    with storage.read_unit(company) as read:
        assert read.head == 0 and app.identity.get_batch([account["object_id"]], read) == ()
    saved = app.accounting_apply(request(company, statement, objects=[account], sources=[evidence]))
    assert saved["completion"] == "complete", saved
    business = get(app, company, statement["business_id"])["businesses"][0]
    assert business["fact"]["evidence_ids"] == []
    assert [item["source_id"] for item in business["sources"]] == [evidence["source_id"]]
    root, manifest = document_set(app, company)
    parts = [
        json.loads((root / shard["path"]).read_text(encoding="utf-8"))
        for shard in manifest["shards"]
    ]
    assert any(part.get("facts") == [statement] for part in parts)
    assert any(
        any(item["source_id"] == evidence["source_id"] for item in part.get("sources", []))
        for part in parts
    )
