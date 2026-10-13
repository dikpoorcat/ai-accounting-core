"""Fresh source-confirmed deductions compile before remuneration publication."""

import copy

import pytest
from test_replay_document import compile_document, document

from ai_accounting.kernel.replay_document import ReplayDocumentBuilder, ReplayDocumentCompileError


def remuneration_document(kind="payroll", tax_fen=0):
    value = document()
    value["entities"] = [
        {
            "key": "worker",
            "kind": "person",
            "data": {"display_name": "Synthetic worker"},
            "sources": ["owner-original"],
        }
    ]

    def add(key, fact_kind, data, dependencies=()):
        value["facts"].append(
            {
                "key": key,
                "kind": fact_kind,
                "data": data,
                "source_keys": ["owner-original"],
                "depends_on": list(dependencies),
            }
        )

    person = "$ref:worker:entity_id"
    common = {"period": "2026-01", "expense_class": "management"}
    if kind in {"payroll", "payroll_bounded"}:
        from test_confirmed_remuneration import wage_case

        _, (profile, policy, _) = wage_case(period="2026-01")
        profile_data = profile.fact.model_dump(mode="json") | {"employee_id": person}
        add("profile", "payroll_profile", profile_data, ["worker"])
        add("policy", "payroll_contribution_policy", policy.fact.model_dump(mode="json"))
        data = common | {
            "employee_id": person,
            "accounting_gross_salary_fen": 100_000,
            "profile_revision": {
                "subject_id": "$ref:profile:subject_id",
                "revision": "$ref:profile:revision",
            },
            "contribution_policy_revision": {
                "subject_id": "$ref:policy:subject_id",
                "revision": "$ref:policy:revision",
            },
            "contribution_basis": "policy_until_actual",
        }
        add("wage", kind, data, ["worker", "profile", "policy"])
        add(
            "plan",
            "payroll_plan_v2",
            {
                "period": "2026-01",
                "payroll_revision": {
                    "subject_id": "$ref:wage:subject_id",
                    "revision": "$ref:wage:revision",
                },
            },
            ["wage"],
        )
    elif kind == "annual_bonus":
        add(
            "wage",
            kind,
            common
            | {
                "employee_id": person,
                "income_date": "2026-01-12",
                "bonus_fen": 100_000,
            },
            ["worker"],
        )
    else:
        add(
            "wage",
            kind,
            common
            | {
                "person_id": person,
                "income_date": "2026-01-12",
                "remuneration_method": "fixed",
                "fixed_fee_fen": 100_000,
            },
            ["worker"],
        )
    add(
        "tax",
        "remuneration_tax_basis",
        {
            "period": "2026-01",
            "source_kind": kind,
            "source_id": "$ref:wage:subject_id",
            "source_fact_id": "$ref:wage:fact_id",
            "tax_period": "2025-12",
            "tax_amount_fen": tax_fen,
            "basis_type": "final_statement",
            "confirmed": True,
        },
        ["wage"],
    )
    return value


def node(compiled, key):
    return next(
        step
        for step in compiled["request"]["steps"]
        if step["step_id"] == ReplayDocumentBuilder.node_id(key)
    )


@pytest.mark.parametrize("kind", ["payroll", "payroll_bounded", "annual_bonus", "labor"])
@pytest.mark.parametrize("tax_fen", [0, 500])
def test_tax_basis_consumes_saved_version_and_precedes_adoption(kind, tax_fen):
    compiled = compile_document(remuneration_document(kind, tax_fen))
    save, basis = node(compiled, "fact-save:wage"), node(compiled, "fact-save:tax")
    preview, confirm = (
        node(compiled, "publication-preview:wage"),
        node(compiled, "publication-confirm:wage"),
    )
    assert save["step_id"] in basis["depends_on"]
    assert confirm["step_id"] not in basis["depends_on"]
    assert basis["step_id"] in preview["depends_on"]
    assert basis["payload"]["facts"][0]["data"]["source_fact_id"] == {
        "$replay_ref": save["output_ref"],
        "field": "results.0.fact_id",
    }
    assert basis["payload"]["facts"][0]["data"]["tax_period"] == "2025-12"
    if kind.startswith("payroll"):
        assert basis["step_id"] in node(compiled, "payroll-prepare:2026-01")["depends_on"]
    # Preparation validates the entire graph, not just the locally inspected edges.
    from ai_accounting.kernel.replay_contracts import ReplayPrepare
    from ai_accounting.kernel.replay_runner import compile_replay_steps
    from ai_accounting.kernel.types import canonical

    ordered, _ = compile_replay_steps(
        ReplayPrepare.model_validate_json(canonical(compiled["request"])).steps
    )
    ids = [step.step_id for step in ordered]
    assert ids.index(save["step_id"]) < ids.index(basis["step_id"]) < ids.index(preview["step_id"])


@pytest.mark.parametrize("change", ["kind", "period", "exact_source", "missing", "duplicate"])
def test_conflicting_or_missing_tax_basis_blocks_compilation(change):
    value = remuneration_document()
    tax = value["facts"][-1]
    if change == "kind":
        tax["data"]["source_kind"] = "labor"
    elif change == "period":
        tax["data"]["period"] = "2026-02"
    elif change == "exact_source":
        tax["data"]["source_fact_id"] = "$ref:opening:zero:fact_id"
    elif change == "missing":
        value["facts"].pop()
    else:
        value["facts"].append(copy.deepcopy(tax) | {"key": "second-tax"})
    with pytest.raises(ReplayDocumentCompileError) as exc:
        compile_document(value)
    codes = {issue["code"] for issue in exc.value.issues}
    assert codes & {
        "remuneration_tax_source_conflict",
        "remuneration_tax_basis_missing",
        "ambiguous_remuneration_tax_basis",
    }


def test_historical_only_kind_is_not_a_fresh_registration_template():
    from test_payroll import income_tax_policy

    value = document()
    value["facts"].append(
        {
            "key": "old-policy",
            "kind": "payroll_income_tax_policy",
            "data": income_tax_policy().model_dump(mode="json"),
            "source_keys": ["owner-original"],
            "depends_on": [],
        }
    )
    with pytest.raises(ReplayDocumentCompileError) as exc:
        compile_document(value)
    assert any(issue["code"] == "fact_kind_not_registerable" for issue in exc.value.issues)


@pytest.mark.parametrize("wrong_peer", [False, True])
def test_pooled_tax_waits_for_all_saved_sources_without_publication_cycle(wrong_peer):
    value = remuneration_document("labor", 500)
    peer = copy.deepcopy(value["facts"][-2]) | {"key": "peer"}
    peer_tax = copy.deepcopy(value["facts"][-1]) | {
        "key": "peer-tax",
        "depends_on": ["peer", "wage"],
    }
    peer_tax["data"] |= {"source_id": "$ref:peer:subject_id", "source_fact_id": "$ref:peer:fact_id"}
    value["facts"].extend([peer, peer_tax])
    proof = {
        "evidence_digest": "a" * 64,
        "source_location": "Synthetic table rows 1-2",
        "total_tax_fen": 1000,
        "allocations": [
            {
                "source_kind": "labor",
                "source_id": "$ref:wage:subject_id",
                "period": "2026-01",
                "tax_amount_fen": 500,
            },
            {
                "source_kind": "annual_bonus" if wrong_peer else "labor",
                "source_id": "$ref:peer:subject_id",
                "period": "2026-01",
                "tax_amount_fen": 500,
            },
        ],
    }
    value["facts"][-3]["data"]["pooled_tax_proof"] = proof
    peer_tax["data"]["pooled_tax_proof"] = proof
    if wrong_peer:
        with pytest.raises(ReplayDocumentCompileError) as exc:
            compile_document(value)
        assert any(
            issue["code"] == "remuneration_tax_pool_source_conflict" for issue in exc.value.issues
        )
    else:
        compiled = compile_document(value)
        tax = node(compiled, "fact-save:tax")
        assert node(compiled, "fact-save:peer")["step_id"] in tax["depends_on"]
        assert node(compiled, "publication-confirm:peer")["step_id"] not in tax["depends_on"]


def test_accrued_labor_does_not_require_or_become_a_tax_confirmed_payment():
    value = document()
    value["entities"] = [
        {
            "key": "worker",
            "kind": "person",
            "data": {"display_name": "Synthetic worker"},
            "sources": ["owner-original"],
        }
    ]
    value["facts"].append(
        {
            "key": "accrual",
            "kind": "labor_accrual",
            "data": {
                "period": "2026-01",
                "person_id": "$ref:worker:entity_id",
                "expense_class": "management",
                "gross_fee_fen": 100_000,
                "tax_treatment": "not_withheld_not_filed",
            },
            "source_keys": ["owner-original"],
            "depends_on": ["worker"],
        }
    )
    compiled = compile_document(value)
    assert node(compiled, "fact-save:accrual")["payload"]["facts"][0]["kind"] == "labor_accrual"
