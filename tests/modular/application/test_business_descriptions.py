"""Source-backed descriptions survive accounting changes and document rebuilds."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.host.public import create_offline_application
from ai_accounting_modular.records.public import open_standard_bundle

from ._working_case import case as case
from .conftest import source
from .test_isolated_targets import read_target


def note(fact, evidence, text="维修费报销", seq=0):
    return {
        "scope": "business:" + fact["business_id"],
        "expected_seq": seq,
        "text": text,
        "evidence_ids": [evidence] if text else [],
    }


def current(case, fact):
    response = case.context(note_scopes=["business:" + fact["business_id"]], issue_scopes=[])
    assert response["note_basis_heads"] == []
    return response["notes"][0]


def bundle(case):
    with case.kernel.storage.read_unit(case.company) as read:
        root = case.kernel.storage.company_metadata(read).company_root
    return open_standard_bundle(root / "standard/current.json")


def test_description_can_join_creation_and_survives_amount_and_source_correction(case):
    case.seed(expense=False)
    fact = case.expense()
    request = case.request(fact, notes=[note(fact, case.source_id)])
    created = case.app.accounting_apply(request)
    assert created["completion"] == "complete", created
    assert current(case, fact)["basis_head"] is None
    before = current(case, fact)
    correction = source("合成最终确认：维修费更正为1200元，用途仍为维修。")
    changed = deepcopy(fact)
    changed["data"]["amount_fen"] = "120000"
    changed["evidence_ids"] = [correction["source_id"]]
    request = case.request(changed, sources=[correction])
    request["mutations"][0].update(op="replace", expected_seq=1)
    response = case.app.accounting_apply(request)
    assert response["completion"] == "complete", response
    assert current(case, fact) == before
    assert bundle(case).notes == (
        {
            "scope": before["scope"],
            "text": "维修费报销",
            "evidence_ids": [case.source_id],
        },
    )


def test_unregistered_note_source_is_input_error_and_rolls_back_the_business(case):
    case.seed(expense=False)
    fact = case.expense()
    missing = str(uuid4())
    rejected = case.app.accounting_apply(case.request(fact, notes=[note(fact, missing)]))
    assert rejected["completion"] == "not_saved", rejected
    assert rejected["error"] == "invalid_input"
    assert [issue["code"] for issue in rejected["fact_issues"]] == ["source_not_found"]
    assert missing in rejected["fact_issues"][0]["message"]
    assert case.get(fact["business_id"])["missing_business_ids"] == [fact["business_id"]]
    assert case.context(note_scopes=["business:" + fact["business_id"]], issue_scopes=[])[
        "notes"
    ] == []


def test_closed_description_only_edit_never_evaluates_or_changes_formal_accounting(
    case, monkeypatch
):
    fact = case.seed()
    assert case.close(case.check())["state"] == "closed"
    before = case.get(fact["business_id"])
    evidence = source("合成确认：这笔维修费是个人垫付后的公司报销。")

    def unexpected(*args, **kwargs):
        raise AssertionError("独立编辑事项说明不能重新核算或读取月度财报。")

    monkeypatch.setattr(case.app.registry.owner(fact["kind"]), "evaluate_slot", unexpected)
    monkeypatch.setattr(case.kernel.ledger, "financial_snapshot", unexpected)
    request = case.request(sources=[evidence], notes=[note(fact, evidence["source_id"])])
    response = case.app.accounting_apply(request)
    assert response["completion"] == "complete", response
    assert response["accounting"] == "unchanged"
    after = case.get(fact["business_id"])
    assert after["businesses"] == before["businesses"]
    assert current(case, fact)["seq"] == 1
    assert case.app.accounting_apply(request)["changes"] == "none"
    with case.kernel.storage.read_unit(case.company) as read:
        assert case.kernel.ledger.closed_through(read) == "2026-01"
        case.app.backup_verifier.verify_snapshot(read)


def test_explicit_clear_and_withdraw_remove_only_current_prose_and_preserve_sequence(case):
    fact = case.seed()
    assert (
        case.app.accounting_apply(case.request(notes=[note(fact, case.source_id)]))["completion"]
        == "complete"
    )
    before = case.get(fact["business_id"])["businesses"]
    cleared = case.app.accounting_apply(case.request(notes=[note(fact, case.source_id, "", 1)]))
    assert cleared["completion"] == "complete", cleared
    assert current(case, fact)["seq"] == 2
    assert current(case, fact)["text"] is None
    assert bundle(case).notes == ()
    assert case.get(fact["business_id"])["businesses"] == before
    assert (
        case.app.accounting_apply(case.request(notes=[note(fact, case.source_id, seq=2)]))[
            "completion"
        ]
        == "complete"
    )
    withdrawn = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "withdraw", "business_id": fact["business_id"], "expected_seq": 1}]
        )
    )
    assert withdrawn["completion"] == "complete", withdrawn
    assert current(case, fact)["seq"] == 4
    assert current(case, fact)["text"] is None
    assert bundle(case).notes == ()
    request = case.request(fact)
    request["mutations"][0].update(op="restore", expected_seq=2)
    assert case.app.accounting_apply(request)["completion"] == "complete"
    assert bundle(case).notes == ()
    stale = case.app.accounting_apply(case.request(notes=[note(fact, case.source_id)]))
    assert stale["completion"] == "not_saved" and stale["error"] == "conflict"


@pytest.mark.parametrize("state", ["missing", "withdrawn"])
def test_invalid_description_identity_rolls_back_sources_and_new_business(case, state):
    fact = case.seed()
    if state == "withdrawn":
        response = case.app.accounting_apply(
            case.request(
                mutations=[
                    {"op": "withdraw", "business_id": fact["business_id"], "expected_seq": 1}
                ]
            )
        )
        assert response["completion"] == "complete", response
    else:
        fact = {**fact, "business_id": str(uuid4())}
    extra = source("合成身份不成立时这份来源不得落库。")
    new = case.expense("12345")
    response = case.app.accounting_apply(
        case.request(new, sources=[extra], notes=[note(fact, extra["source_id"])])
    )
    assert response["status"] == "rejected" and response["code"] == "not_found", response
    assert case.get(new["business_id"])["missing_business_ids"] == [new["business_id"]]
    with case.kernel.storage.read_unit(case.company) as read:
        assert case.kernel.records.resolve_sources([extra["source_id"]], read) == ()


def test_final_description_and_source_survive_real_empty_target_rebuild(case, tmp_path):
    fact = case.seed()
    assert (
        case.app.accounting_apply(case.request(notes=[note(fact, case.source_id)]))["completion"]
        == "complete"
    )
    documents = bundle(case)
    target = tmp_path / "description-rebuilt"
    offline = create_offline_application()
    response = offline.accounting_isolated(
        {
            "action": "rebuild",
            "mode": "run",
            "source_path": str(documents.manifest_path),
            "new_target_directory": str(target),
        }
    )
    assert response["state"] == "created", response
    rebuilt = open_standard_bundle(target / "standard/current.json")
    assert rebuilt.facts == documents.facts
    assert rebuilt.notes == documents.notes
    assert rebuilt.sources == documents.sources
    with read_target(offline, target) as read:
        description = offline.records.current_notes(("business:" + fact["business_id"],), read)[0]
        assert description["text"] == "维修费报销" and description["basis_head"] is None
        result = offline.ledger.get_results([fact["business_id"]], read)[0]
        assert result["target"]["values"]["amount_fen"] == "100000"
        assert {line["account_code"] for line in result["vouchers"][0]["lines"]} == {"5602", "2202"}
        offline.backup_verifier.verify_snapshot(read)
