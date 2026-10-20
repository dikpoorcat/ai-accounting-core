"""Saved complete responsibilities prove missing outputs without rerunning old formulas."""

from copy import deepcopy
from json import loads
from uuid import uuid4

from ai_accounting_modular.application._repository import ApplicationRepository
from ai_accounting_modular.contracts.public import ContractError

from ._working_case import case as case
from .test_period_coordination import asset_case


def heads(case, *ids):
    with case.kernel.storage.read_unit(case.company) as read:
        return read.repository("application").heads(ids)


def test_explicit_current_responsibility_follows_replace_withdraw_and_restore(case):
    fact = case.seed()
    business = fact["business_id"]
    assert loads(heads(case, business)[business]["result_slots_json"]) == ["main"]
    changed = deepcopy(fact)
    changed["data"]["amount_fen"] = "90000"
    request = case.request(changed)
    request["mutations"][0].update(op="replace", expected_seq=1)
    assert case.app.accounting_apply(request)["completion"] == "complete"
    assert loads(heads(case, business)[business]["result_slots_json"]) == ["main"]
    withdrawal = case.request()
    withdrawal["mutations"] = [{"op": "withdraw", "business_id": business, "expected_seq": 2}]
    assert case.app.accounting_apply(withdrawal)["completion"] == "complete"
    assert loads(heads(case, business)[business]["result_slots_json"]) == []
    request["command_id"] = str(uuid4())
    request["mutations"][0].update(op="restore", expected_seq=3)
    assert case.app.accounting_apply(request)["completion"] == "complete"
    assert loads(heads(case, business)[business]["result_slots_json"]) == ["main"]
    assert (
        case.get(business)["businesses"][0]["results"][0]["target"]["values"]["amount_fen"]
        == "90000"
    )
    before = heads(case, business)[business]
    assert case.app.accounting_apply(request)["changes"] == "none"
    assert heads(case, business)[business] == before


def test_horizon_updates_all_slots_without_changing_fact_sequence_and_preview_rolls_back(case):
    _, enabled = asset_case(case)
    business = enabled["business_id"]
    before = heads(case, business)[business]
    assert loads(before["result_slots_json"]) == ["main"]
    checked = case.check("2026-03")
    assert checked["ready"], checked
    assert heads(case, business)[business] == before
    closed = case.close(checked)
    assert closed["state"] == "closed", closed
    after = heads(case, business)[business]
    assert loads(after["result_slots_json"]) == [
        "depreciation:2026-02",
        "depreciation:2026-03",
        "main",
    ]
    assert after["seq"] == before["seq"] == 1
    assert after["search_revision"] == before["search_revision"]
    references = case.get(business)["businesses"][0]["results"]
    assert {view["reference"]["result_slot"] for view in references} == set(
        loads(after["result_slots_json"])
    )


def test_changed_commitment_is_reported_even_when_formal_result_is_reused(case):
    fact = case.seed()
    business = fact["business_id"]
    formal = case.get(business)["businesses"]
    # Isolated damage to an application-owned projection; the ordinary typed
    # retry re-establishes the owner's declaration and reuses the same result.
    with case.kernel.storage.write_unit(case.company) as unit:
        assert unit.repository("application").replace_result_slots(
            {business: {"complete_result_slots": []}}
        )
    before = heads(case, business)[business]
    result = case.app.accounting_apply(case.request(fact))
    assert result["completion"] == "complete", result
    assert result["changes"] == "committed" and result["accounting"] == "unchanged"
    assert result["reading"] == "unchanged"
    assert case.get(business)["businesses"] == formal
    after = heads(case, business)[business]
    assert loads(after["result_slots_json"]) == ["main"]
    assert after["seq"] == before["seq"]
    assert after["search_revision"] == before["search_revision"]
    assert case.app.accounting_apply(case.request(fact))["changes"] == "none"


def test_late_responsibility_failure_rolls_back_new_slots_and_new_business(case, monkeypatch):
    _, enabled = asset_case(case)
    business = enabled["business_id"]
    before_head = heads(case, business)[business]
    before_formal = case.get(business)
    replace = ApplicationRepository.replace_result_slots

    def fail_after_update(repository, responsibilities):
        assert replace(repository, responsibilities)
        raise ContractError("temporary_failure", "完整结果责任保存后发生合成故障。")

    monkeypatch.setattr(ApplicationRepository, "replace_result_slots", fail_after_update)
    added = case.expense("30000", "2026-03")
    failed = case.app.accounting_apply(case.request(added))
    assert failed["completion"] == "not_saved" and failed["error"] == "temporary_failure", failed
    assert heads(case, business)[business] == before_head
    assert case.get(business) == before_formal
    assert case.get(added["business_id"])["missing_business_ids"] == [added["business_id"]]
