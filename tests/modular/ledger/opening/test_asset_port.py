"""Opening coordinates the public asset contract without taking over its formulas."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.assets.public import create_assets
from ai_accounting_modular.ledger.opening.public import create_opening
from ai_accounting_modular.ledger.public import LedgerQuery

from ._support import (
    FIXED,
    PARTY,
    SOURCE,
    assemble,
    bank_and_equity,
    get,
    loan_opening,
    member,
    opening,
    replace,
    request,
    source,
)


def card_opening():
    return opening(
        member(
            {
                "type": "asset",
                "asset_id": FIXED,
                "asset_type": "fixed",
                "cost_fen": "12000",
                "accumulated_fen": "5000",
                "in_use_period": "2026-01",
                "useful_life_months": 12,
                "completed_months": 5,
                "residual_fen": "0",
                "benefit_area": "administration",
                "rounding": "floor_final_remainder",
            }
        ),
        member(
            {
                "type": "equity",
                "equity_kind": "retained_earnings",
                "holder_or_basis_id": PARTY,
                "balance_fen": "7000",
            }
        ),
    )


def additional(fact):
    slot = "depreciation:2026-07"
    return {
        "complete_result_slots": [slot],
        "dependencies": [
            {
                "consumer_business_id": fact["business_id"],
                "consumer_result_slot": slot,
                "producer_kind": "business",
                "producer_id": fact["business_id"],
                "producer_public_slot": "main",
                "read_mode": "result",
                "relation_kind": "calculation",
            }
        ],
        "publications": [
            {
                "business_id": fact["business_id"],
                "result_slot": slot,
                "collection": "pre_tax",
                "period_from": "2026-07",
                "period_to": "2026-07",
            }
        ],
        "issues": [],
    }


class FaultyDescription:
    def __init__(self, fault):
        self.fault = fault

    def describe_opening_assets(self, fact, context):
        result = additional(fact)
        if self.fault == "main":
            result["complete_result_slots"] = ["main"]
        elif self.fault == "duplicate":
            result["complete_result_slots"] *= 2
        elif self.fault == "non_slot":
            result["complete_result_slots"] = [{}]
        elif self.fault == "missing_field":
            result.pop("issues")
        elif self.fault == "foreign_consumer":
            result["dependencies"][0]["consumer_business_id"] = str(uuid4())
        elif self.fault == "unowned_consumer":
            result["dependencies"][0]["consumer_result_slot"] = "unowned"
        elif self.fault == "foreign_publication":
            result["publications"][0]["business_id"] = str(uuid4())
        elif self.fault == "unowned_publication":
            result["publications"][0]["result_slot"] = "unowned"
        elif self.fault == "issue":
            result["issues"].append(
                {
                    "code": "missing_fact",
                    "business_id": fact["business_id"],
                    "field": "/data/members/0/data",
                    "meaning": "资产接续必要依据",
                    "message": "合成的资产来源缺项。",
                    "allowed_precision": ["source"],
                    "reusable_sources": [{"source_id": SOURCE}],
                    "blocking_scope": "business",
                }
            )
        return result

    def evaluate_opening_asset_slot(self, fact, result_slot, context):
        raise AssertionError("An invalid description must never be evaluated")

    def opening_asset_cards(self, fact, context):
        raise AssertionError("An invalid description must never produce opening cards")

    def validate_opening_asset_candidate(self, plan, context):
        raise AssertionError("An invalid description must never reach final validation")


@pytest.mark.parametrize(
    "fault",
    [
        "main",
        "duplicate",
        "non_slot",
        "missing_field",
        "foreign_consumer",
        "unowned_consumer",
        "foreign_publication",
        "unowned_publication",
        "issue",
    ],
)
def test_asset_description_preserves_openings_ownership_and_complete_slots(tmp_path, fault):
    app, company = assemble(tmp_path, asset_accounting=FaultyDescription(fault))
    item = card_opening()
    response = app.accounting_apply(request(company, item))
    assert response.get("completion") == "not_saved", response
    assert response["error"] == ("needs_information" if fault == "issue" else "invalid_input")
    assert get(app, company, item["business_id"])["businesses"] == []
    with app.storage.read_unit(company) as read:
        assert read.head == 0


def test_cards_cannot_be_silently_saved_without_their_asset_accounting_port(tmp_path):
    app, company = assemble(tmp_path)
    item = card_opening()
    response = app.accounting_apply(request(company, item))
    assert response.get("completion") == "not_saved", response
    assert response["error"] == "unsupported_business"
    assert get(app, company, item["business_id"])["businesses"] == []


class WrongPlanIdentity:
    def __init__(self, fault):
        self.fault = fault

    def describe_opening_assets(self, fact, context):
        return additional(fact)

    def opening_asset_cards(self, fact, context):
        return create_assets().opening_asset_cards(fact, context)

    def evaluate_opening_asset_slot(self, fact, result_slot, context):
        # This deliberately broken adapter copies a valid plan solely to test
        # the ownership boundary. It never commits a substitute asset result.
        plan = create_opening(asset_accounting=self).evaluate_slot(
            fact["business_id"], "main", context
        )
        slot = result_slot if self.fault == "business" else "other-slot"
        plan["result_slot"] = slot
        plan["target"]["result_slot"] = slot
        plan["basis"]["result_slot"] = slot
        if self.fault == "business":
            plan["business_id"] = str(uuid4())
        return plan

    def validate_opening_asset_candidate(self, plan, context):
        raise AssertionError("A foreign plan must never reach final validation")


@pytest.mark.parametrize("fault", ["business", "slot"])
def test_delegated_asset_plan_cannot_change_business_or_slot_identity(tmp_path, fault):
    app, company = assemble(tmp_path, asset_accounting=WrongPlanIdentity(fault))
    item = card_opening()
    response = app.accounting_apply(request(company, item))
    assert response.get("completion") == "not_saved", response
    assert response["error"] == "invalid_input"
    assert get(app, company, item["business_id"])["businesses"] == []


class CandidateObserver:
    def __init__(self, *, reject=False):
        self.plan = None
        self.reject = reject

    def describe_opening_assets(self, fact, context):
        raise AssertionError("An opening without cards needs no asset description")

    def evaluate_opening_asset_slot(self, fact, result_slot, context):
        raise AssertionError("An opening without cards has no added asset slots")

    def opening_asset_cards(self, fact, context):
        raise AssertionError("An opening without cards needs no card calculation")

    def validate_opening_asset_candidate(self, plan, context):
        self.plan = deepcopy(plan)
        if self.reject:
            raise ContractError("dependency_conflict", "合成的完整资产候选冲突。")


@pytest.mark.parametrize("reject", [False, True])
def test_asset_validator_receives_the_complete_opening_plan_and_can_reject_atomically(
    tmp_path, reject
):
    observer = CandidateObserver(reject=reject)
    app, company = assemble(tmp_path, asset_accounting=observer)
    item = bank_and_equity()
    response = app.accounting_apply(request(company, item))
    assert observer.plan["domain"] == "opening"
    assert observer.plan["businesses"][0]["fact"] == item
    assert observer.plan["businesses"][0]["complete_result_slots"] == ["main"]
    assert len(observer.plan["plans"]) == 1
    assert observer.plan["plans"][0]["target"]["kind"] == "opening.package"
    if reject:
        assert response["completion"] == "not_saved"
        assert get(app, company, item["business_id"])["businesses"] == []
    else:
        assert response["completion"] == "complete", response


class FaultyCards:
    def __init__(self, fault):
        self.fault = fault
        self.assets = create_assets()

    def describe_opening_assets(self, fact, context):
        return self.assets.describe_opening_assets(fact, context)

    def opening_asset_cards(self, fact, context):
        cards = self.assets.opening_asset_cards(fact, context)
        if self.fault == "missing":
            return ()
        if self.fault == "duplicate":
            return cards * 2
        if self.fault == "not_tuple":
            return list(cards)
        card = deepcopy(cards[0])
        if self.fault == "foreign":
            card["asset_id"] = str(uuid4())
        elif self.fault == "type":
            card["asset_type"] = "intangible"
        else:
            # Preserving the net balance must not hide wrong gross balances.
            card["cost_fen"] = str(int(card["cost_fen"]) + 1)
            card["accumulated_fen"] = str(int(card["accumulated_fen"]) + 1)
        return (card,)

    def evaluate_opening_asset_slot(self, fact, result_slot, context):
        raise AssertionError("Invalid cards must stop before monthly calculations")

    def validate_opening_asset_candidate(self, plan, context):
        raise AssertionError("Invalid cards must stop before final validation")


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "not_tuple", "foreign", "type", "amount"]
)
def test_public_asset_cards_are_complete_and_keep_the_sourced_opening_identity(tmp_path, fault):
    app, company = assemble(tmp_path, asset_accounting=FaultyCards(fault))
    item = card_opening()
    response = app.accounting_apply(request(company, item))
    assert response.get("completion") == "not_saved", response
    assert response["error"] == "invalid_input"
    assert get(app, company, item["business_id"])["businesses"] == []


def views(app, company, item):
    return {
        result["reference"]["result_slot"]: result
        for result in get(app, company, item["business_id"])["businesses"][0]["results"]
    }


def test_real_asset_slots_advance_with_work_then_exit_as_one_complete_opening_responsibility(
    tmp_path,
):
    app, company = assemble(tmp_path, asset_accounting=create_assets())
    item = card_opening()
    first = app.accounting_apply(request(company, item))
    assert first.get("completion") == "complete", first
    before = views(app, company, item)
    assert set(before) == {"main", "depreciation:2026-07"}
    assert before["main"]["vouchers"] == []
    july = before["depreciation:2026-07"]
    assert july["target"]["kind"] == "assets.consumption"
    assert sum(int(line["debit_fen"]) for line in july["vouchers"][0]["lines"]) == 1000
    future, _, _ = loan_opening(contract_period="2026-08")
    new_source = str(uuid4())
    future["evidence_ids"] = [new_source]
    submitted = request(company, future)
    evidence = source(new_source)
    evidence["archive_month"] = "2026-08"
    submitted["sources"] = [evidence]
    advanced = app.accounting_apply(submitted)
    assert advanced.get("completion") == "complete", advanced
    after = views(app, company, item)
    assert set(after) == {"main", "depreciation:2026-07", "depreciation:2026-08"}
    assert after["depreciation:2026-07"]["reference"] == july["reference"]
    august = after["depreciation:2026-08"]
    assert sum(int(line["debit_fen"]) for line in august["vouchers"][0]["lines"]) == 1000
    corrected = bank_and_equity("7000")
    corrected["business_id"] = item["business_id"]
    replaced = app.accounting_apply(replace(company, corrected))
    assert replaced.get("completion") == "complete", replaced
    assert set(views(app, company, corrected)) == {"main"}
    with app.storage.read_unit(company) as read:
        assert app.ledger.query_vouchers(LedgerQuery((item["business_id"],)), read).items == ()


def test_withdrawing_opening_with_asset_slots_leaves_no_old_monthly_results(tmp_path):
    app, company = assemble(tmp_path, asset_accounting=create_assets())
    item = card_opening()
    assert app.accounting_apply(request(company, item))["completion"] == "complete"
    withdrawn = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [{"op": "withdraw", "expected_seq": 1, "business_id": item["business_id"]}],
    }
    response = app.accounting_apply(withdrawn)
    assert response.get("completion") == "complete", response
    stored = get(app, company, item["business_id"])["businesses"][0]
    assert stored["state"]["state"] == "withdrawn"
    assert stored["results"] == []
