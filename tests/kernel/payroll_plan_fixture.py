"""Explicit synthetic owner confirmations for integration-test wage scenarios.

Tests call this after arranging the wage and its sources. Nothing is installed
in production, a global fixture, or the publication path; tests of missing or
stale confirmation deliberately omit the call.
"""

from ai_accounting.kernel.contracts import Read
from ai_accounting.kernel.payroll_confirmation import revision_reference
from ai_accounting.kernel.payroll_preparation import PayrollPlan
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
        notices = engine.store.select(
            connection,
            Read(
                "fact",
                "payroll_change_notice_v2",
                f"employee:{wage.employee_id}:month:{wage.period}",
            ),
        )
        model = PayrollPlan
        plan = model(
            period=wage.period,
            payroll_revision=revision_reference(versions[0]),
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
