"""Identity corrections bind explicitly confirmed tax to the same new source revision."""

import copy

import pytest
from test_payroll import contribution_policy, payroll, profile
from test_payroll_corrections import Company

from ai_accounting.kernel.contracts import KernelError, Read
from ai_accounting.kernel.identity_corrections import IdentityCorrections
from ai_accounting.kernel.integrity import verify_integrity


def prepared(tmp_path, closed):
    company = Company(tmp_path / "identity-tax.sqlite")
    company.save(contribution_policy(), "contributions")
    company.save(
        profile(employee_id="first", social_insurance_participating=False), "first-profile"
    )
    wage = payroll(employee_id="first", profile_id="first-profile")
    company.save(wage, "wage")
    company.confirm_payroll("wage")
    company.tax_basis("wage", 12_345)
    company.publish("wage")
    frozen = company.close("2026-01") if closed else None
    company.save(
        profile(employee_id="second", social_insurance_participating=False), "second-profile"
    )
    with company.engine.store.connection(read_only=True) as connection:
        plan = company.engine.store.select(
            connection, Read("fact", "payroll_plan_v2", "payroll-confirmation:wage")
        )[0]
        basis = company.engine.store.current_fact(connection, "tax-basis-wage")
    changed_wage = wage.model_dump(mode="json") | {
        "employee_id": "second",
        "profile_revision": {"subject_id": "second-profile", "revision": 1},
    }
    changed_plan = plan.fact.model_dump(mode="json") | {
        "payroll_revision": {"subject_id": "wage", "revision": 2},
    }
    kwargs = {
        "changes": [
            {
                "subject_id": "tax-basis-wage",
                "expected_revision": 1,
                "action": "reassign",
                "data": basis.fact.model_dump(mode="json"),
                "remuneration_source_revision": {"subject_id": "wage", "revision": 2},
            },
            {
                "subject_id": "wage",
                "expected_revision": 1,
                "action": "reassign",
                "data": changed_wage,
            },
            {
                "subject_id": plan.subject_id,
                "expected_revision": 1,
                "action": "reassign",
                "data": changed_plan,
            },
        ],
        "evidence": [company.owner_confirmation],
        "reason": "同一人员身份明确纠正，同时确认本项工资扣税仍为12345分",
        "posting_period": "2026-02" if closed else None,
    }
    return company, kwargs, frozen


@pytest.mark.parametrize("closed", [False, True])
def test_explicit_companion_binds_generated_fact_before_publication_and_preserves_history(
    tmp_path, closed
):
    company, kwargs, frozen = prepared(tmp_path, closed)
    command = IdentityCorrections(company.engine)
    preview = command.preview_identity_correction(**kwargs)
    facts = {item["subject_id"]: item for item in preview["fact_changes"]}
    assert facts["tax-basis-wage"]["data"]["source_fact_id"] == facts["wage"]["fact_id"]
    result = next(item for item in preview["results"] if item["subject_id"] == "wage")
    assert result["values"]["tax_fen"] == 12_345
    assert result["values"]["employee_id"] == "second"
    command.confirm_identity_correction(
        **kwargs,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="correct-explicit-tax",
    )
    with company.engine.store.connection(read_only=True) as connection:
        source = company.engine.store.current_fact(connection, "wage")
        basis = company.engine.store.current_fact(connection, "tax-basis-wage")
        assert basis.fact.source_fact_id == source.id
        assert source.revision == basis.revision == 2
        verify_integrity(company.engine, connection)
    if closed:
        from ai_accounting.kernel.periods import Periods

        assert Periods(company.engine).closed_report("2026-01") == frozen


@pytest.mark.parametrize(
    "problem",
    ["missing_target", "wrong_revision", "wrong_kind", "amount_change", "missing_binding"],
)
def test_invalid_companion_source_is_atomic_and_does_not_reuse_old_person_tax(tmp_path, problem):
    company, kwargs, _ = prepared(tmp_path, False)
    kwargs = copy.deepcopy(kwargs)
    before = {
        table: company.count(table)
        for table in ("fact_revision", "calculation", "audit", "identity_correction")
    }
    if problem == "missing_target":
        kwargs["changes"][0]["remuneration_source_revision"]["subject_id"] = "no-target"
    elif problem == "wrong_revision":
        kwargs["changes"][0]["remuneration_source_revision"]["revision"] = 3
    elif problem == "wrong_kind":
        kwargs["changes"][1]["subject_id"] = "second-profile"
        kwargs["changes"][1]["data"] = profile(employee_id="first").model_dump(mode="json")
    elif problem == "amount_change":
        kwargs["changes"][0]["data"]["tax_amount_fen"] += 1
    else:
        kwargs["changes"].pop(0)
    with pytest.raises(KernelError):
        IdentityCorrections(company.engine).preview_identity_correction(**kwargs)
    assert {table: company.count(table) for table in before} == before
    assert company.current("wage").values["employee_id"] == "first"


@pytest.mark.parametrize("kind", ["annual_bonus", "labor"])
def test_nonwage_identity_companion_uses_same_exact_source_binding(tmp_path, kind):
    from test_payroll import bonus, labor

    from ai_accounting.kernel.entities import Entities

    company = Company(tmp_path / "nonwage-identity.sqlite")
    person_field = "employee_id" if kind == "annual_bonus" else "person_id"
    fact = bonus(employee_id="first") if kind == "annual_bonus" else labor(person_id="first")
    company.save(fact, "remuneration")
    company.tax_basis("remuneration", 12345)
    company.publish("remuneration")
    second = Entities(company.engine).register_entity(
        "person",
        {},
        source="explicit synthetic correction",
        request_id="second-person",
    )["entity_id"]
    with company.engine.store.connection(read_only=True) as connection:
        basis = company.engine.store.current_fact(connection, "tax-basis-remuneration")
    kwargs = dict(
        changes=[
            dict(
                subject_id="tax-basis-remuneration",
                expected_revision=1,
                action="reassign",
                data=basis.fact.model_dump(mode="json"),
                remuneration_source_revision=dict(subject_id="remuneration", revision=2),
            ),
            dict(
                subject_id="remuneration",
                expected_revision=1,
                action="reassign",
                data=fact.model_dump(mode="json") | {person_field: second},
            ),
        ],
        evidence=[company.owner_confirmation],
        reason="exact confirmed remuneration identity and tax",
    )
    command = IdentityCorrections(company.engine)
    preview = command.preview_identity_correction(**kwargs)
    command.confirm_identity_correction(
        **kwargs,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="nonwage-correction",
    )
    with company.engine.store.connection(read_only=True) as connection:
        source = company.engine.store.current_fact(connection, "remuneration")
        basis = company.engine.store.current_fact(connection, "tax-basis-remuneration")
        assert basis.fact.source_fact_id == source.id
        assert getattr(source.fact, person_field) == second
        verify_integrity(company.engine, connection)
    current = company.current("remuneration", kind=kind)
    assert current.values["tax_fen"] == 12345
