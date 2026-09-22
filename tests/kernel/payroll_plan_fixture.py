"""Explicit synthetic owner confirmations for integration-test wage scenarios.

Tests call this after arranging the wage and its sources. Nothing is installed
in production, a global fixture, or the publication path; tests of missing or
stale confirmation deliberately omit the call.
"""

from ai_accounting.kernel.contracts import Read
from ai_accounting.kernel.payroll_confirmation import revision_reference
from ai_accounting.kernel.payroll_preparation import BoundedPayrollPlan, PayrollPlan
from ai_accounting.kernel.types import digest


def confirm_wage_inputs(engine, subject_id, *, evidence, request_id):
    """Record the caller's explicit synthetic confirmation of current wage inputs."""
    if not evidence:
        raise ValueError("the test must supply its owner-confirmation evidence")
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        versions = [
            item
            for kind in ("payroll", "payroll_bounded")
            for item in engine.store.select(connection, Read("fact", kind, "@" + subject_id))
        ]
        if len(versions) != 1:
            raise ValueError("one current synthetic wage is required")
        wage = versions[0].fact
        references = {}
        profile = None
        for field, kind, source_id in (
            ("profile_revision", "payroll_profile", wage.profile_id),
            (
                "contribution_policy_revision",
                "payroll_contribution_policy",
                wage.contribution_policy_id,
            ),
            ("income_tax_policy_revision", "payroll_income_tax_policy", wage.income_tax_policy_id),
        ):
            if (
                field == "income_tax_policy_revision"
                and profile is not None
                and wage.period < profile.fact.withholding_start_date[:7]
            ):
                references[field] = None
                continue
            sources = engine.store.select(connection, Read("fact", kind, "@" + source_id))
            if len(sources) != 1:
                raise ValueError("arrange every exact payroll source before confirming it")
            references[field] = revision_reference(sources[0])
            if field == "profile_revision":
                profile = sources[0]
        notices = engine.store.select(
            connection,
            Read(
                "fact",
                "payroll_change_notice_v2",
                f"employee:{wage.employee_id}:month:{wage.period}",
            ),
        )
        model = PayrollPlan if wage.kind == "payroll" else BoundedPayrollPlan
        plan = model(
            period=wage.period,
            employee_id=wage.employee_id,
            payroll=wage,
            **references,
            change_notice_revisions=tuple(
                revision_reference(item)
                for item in sorted(notices, key=lambda item: item.subject_id)
            ),
        )
        plan_key = digest([wage.employee_id, str(wage.period)]).hex()[:24]
        plan_subject = "test-wage-confirmation-" + plan_key
        current = engine.store.select(connection, Read("fact", model.kind, "@" + plan_subject))
        revision = current[0].revision if current else 0
        if current and current[0].fact == plan and set(current[0].evidence) == set(evidence):
            return {"fact_id": current[0].id, "subject_id": plan_subject, "revision": revision}
    return engine.save_fact(
        model.kind,
        plan_subject,
        plan.model_dump(mode="json"),
        evidence=tuple(evidence),
        expected_revision=revision,
        request_id=request_id,
    )
