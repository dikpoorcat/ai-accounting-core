"""Pure bank adoption order; original typed inputs and IDs stay untouched."""

from copy import deepcopy

import pytest
from test_replay_document import compile_document, document

from ai_accounting.kernel.replay_bank_dependencies import add_bank_reconciliation_dependencies
from ai_accounting.kernel.replay_document import ReplayDocumentBuilder, ReplayDocumentCompileError


def bank_document():
    value = document()
    value["scope"]["last_period"] = "2026-02"
    value["inventory_replay_plan"]["rows"].extend(
        [
            dict(deepcopy(row), key=row["key"] + ":feb", period="2026-02")
            for row in value["inventory_replay_plan"]["rows"]
        ]
    )
    for account in ("bank-a", "bank-b"):
        value["entities"].append(
            {
                "key": account,
                "kind": "fund_account",
                "account_type": "bank",
                "data": {
                    "display_name": account,
                    "bank_name": "Synthetic bank",
                    "bank_account_number": "12345678901" if account == "bank-a" else "12345678902",
                    "account_holder_name": "Synthetic owner",
                },
                "sources": ["owner-original"],
            }
        )
        value["facts"].append(
            {
                "key": account + ":opening",
                "kind": "bank_opening",
                "data": {
                    "period": "2026-01",
                    "bank_account_id": f"$ref:{account}:entity_id",
                    "opening_fen": 0,
                    "basis": "new_account",
                },
                "source_keys": ["owner-original"],
                "depends_on": [account],
            }
        )
        # Deliberately append February before January: input list order is not
        # evidence that the earlier reconciliation has actually been adopted.
        for month in ("2026-02", "2026-01"):
            statement_key, reconciliation_key = (
                account + ":statement:" + month,
                account + ":rec:" + month,
            )
            value["facts"].extend(
                [
                    {
                        "key": statement_key,
                        "kind": "bank_statement",
                        "data": {
                            "period": month,
                            "bank_account_id": f"$ref:{account}:entity_id",
                            "opening_fen": 0,
                            "closing_fen": 0,
                            "entries": [],
                        },
                        "source_keys": ["owner-original"],
                        "depends_on": [account],
                    },
                    {
                        "key": reconciliation_key,
                        "kind": "bank_reconciliation",
                        "data": {
                            "period": month,
                            "bank_account_id": f"$ref:{account}:entity_id",
                            "statement_id": f"$ref:{statement_key}:subject_id",
                            "matches": [],
                        },
                        "source_keys": ["owner-original"],
                        "depends_on": [account, statement_key],
                    },
                ]
            )
    return value


def test_all_accounts_wait_for_adopted_opening_then_previous_reconciliation():
    compiled = compile_document(bank_document())
    request = compiled["request"]
    steps = {step["step_id"]: step for step in request["steps"]}
    assert len(compiled["bank_reconciliation_edges"]) == 4
    for account in ("bank-a", "bank-b"):
        first = ReplayDocumentBuilder.node_id("publication-preview:" + account + ":rec:2026-01")
        second = ReplayDocumentBuilder.node_id("publication-preview:" + account + ":rec:2026-02")
        opening = ReplayDocumentBuilder.node_id("publication-confirm:" + account + ":opening")
        previous = ReplayDocumentBuilder.node_id("publication-confirm:" + account + ":rec:2026-01")
        assert opening in steps[first]["depends_on"]
        assert previous in steps[second]["depends_on"]
        assert (
            ReplayDocumentBuilder.node_id("fact-save:" + account + ":rec:2026-01")
            not in steps[second]["depends_on"]
        )


def test_saved_request_patch_changes_dependencies_only_and_is_idempotent():
    compiled = compile_document(bank_document())
    original = deepcopy(compiled["request"])
    by_id = {step["step_id"]: step for step in original["steps"]}
    for edge in compiled["bank_reconciliation_edges"]:
        by_id[edge["step_id"]]["depends_on"].remove(edge["depends_on"])
    frozen = deepcopy(original)
    patched = add_bank_reconciliation_dependencies(original)
    assert original == frozen
    assert len(patched["added_edges"]) == 4
    for old_step, new_step in zip(original["steps"], patched["request"]["steps"], strict=True):
        assert {key: value for key, value in old_step.items() if key != "depends_on"} == {
            key: value for key, value in new_step.items() if key != "depends_on"
        }
    for key in original:
        if key != "steps":
            assert original[key] == patched["request"][key]
    assert add_bank_reconciliation_dependencies(patched["request"])["added_edges"] == []


def test_missing_month_is_a_fact_issue_and_is_never_filled_or_skipped():
    value = bank_document()
    value["facts"] = [fact for fact in value["facts"] if fact["key"] != "bank-a:rec:2026-01"]
    with pytest.raises(ReplayDocumentCompileError) as exc:
        compile_document(value)
    assert exc.value.issues[0]["code"] == "bank_reconciliation_start_gap"
    assert not any(fact["key"] == "bank-a:rec:2026-01" for fact in value["facts"])


def test_intermediate_missing_reconciliation_is_not_skipped_to_the_next_available_month():
    value = bank_document()
    value["scope"]["last_period"] = "2026-03"
    value["inventory_replay_plan"]["rows"].extend(
        dict(deepcopy(row), key=row["key"] + ":mar", period="2026-03")
        for row in value["inventory_replay_plan"]["rows"][:6]
    )
    for fact in value["facts"]:
        if fact["key"] in {"bank-a:statement:2026-02", "bank-a:rec:2026-02"}:
            fact["data"]["period"] = "2026-03"
    with pytest.raises(ReplayDocumentCompileError) as exc:
        compile_document(value)
    assert exc.value.issues[0]["code"] == "bank_reconciliation_period_gap"


def test_bank_sources_cannot_be_ambiguously_published():
    request = compile_document(bank_document())["request"]
    original_confirm = next(
        step
        for step in request["steps"]
        if step["step_id"]
        == ReplayDocumentBuilder.node_id("publication-confirm:bank-a:rec:2026-01")
    )
    duplicate = deepcopy(original_confirm)
    duplicate["step_id"] = "duplicate-bank-publication"
    duplicate.pop("output_ref", None)
    duplicate.pop("output_fields", None)
    duplicate.pop("output_selectors", None)
    request["steps"].append(duplicate)
    with pytest.raises(ReplayDocumentCompileError) as exc:
        add_bank_reconciliation_dependencies(request)
    assert exc.value.issues[0]["code"] == "bank_publication_not_unique"
