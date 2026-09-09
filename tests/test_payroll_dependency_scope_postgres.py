"""New source versions invalidate old calculations, not every later payroll."""

import uuid
from datetime import date

import pytest
from _postgres_helpers import authenticated_business_database, confirmed_payroll
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ai_accounting.accounting_periods import canonical_sha256
from ai_accounting.event_amendment_schemas import DeleteEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.models import (
    EmployeePayrollProfileVersion,
    PayrollOpeningState,
    PayrollPolicyVersion,
)
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RegisterPayrollOpeningStateRequest,
)
from ai_accounting.service import FinanceService


def _successor(session, source, **changes):
    table = source.__table__
    values = {column.name: getattr(source, column.name) for column in table.columns}
    values.update(id=uuid.uuid4(), supersedes_id=source.id, **changes)
    values["execution_attribution_id"] = uuid.UUID(
        session.scalar(text("SELECT current_setting('finance.execution_attribution_id')"))
    )
    # A caller must not backdate provenance to make an old calculation appear new.
    values["created_at"] = "1900-01-01T00:00:00Z"
    row_id = session.scalar(table.insert().values(**values).returning(table.c.id))
    result = session.get(type(source), row_id)
    assert result.created_at.year > 1900
    return result


@pytest.mark.parametrize("kind", ["profile", "policy", "opening"])
def test_corrected_source_can_be_used_and_cannot_be_replaced_under_final_payroll(kind):
    with authenticated_business_database("payroll_source_scope") as (
        engine,
        org_id,
        evidence_id,
        owner,
    ):
        with Session(engine) as session:
            _, batch, line, _, event = confirmed_payroll(session, org_id, evidence_id, owner)
            employee_id = line.employee_id
            request = PreviewPayrollRequest.model_validate(batch.calculation_input["request"])
            source_ids = (line.employee_payroll_profile_version_id, batch.policy_version_id)
            session.commit()
            with owner.attributed_call(session, tool_name="finance_delete_event"):
                deleted = EventAmendmentService(session).amend(
                    DeleteEventRequest(
                        org_id=org_id,
                        event_id=event.id,
                        expected_facts_hash=canonical_sha256(event.facts),
                        idempotency_key="withdraw-mistaken-payroll",
                    )
                )
            assert deleted["status"] == "deleted", deleted
            session.commit()
            service = FinanceService(session)
            with owner.attributed_call(session, tool_name="finance_payroll_source_scope_test"):
                if kind == "profile":
                    source = session.get(EmployeePayrollProfileVersion, source_ids[0])
                elif kind == "policy":
                    source = session.get(PayrollPolicyVersion, source_ids[1])
                else:
                    # This test employee began withholding in March, so the
                    # explicitly registered February opening is genuinely zero.
                    values = {
                        field: 0
                        for field in RegisterPayrollOpeningStateRequest.model_fields
                        if field.endswith("_fen")
                    }
                    registered = service.register_payroll_opening_state(
                        RegisterPayrollOpeningStateRequest(
                            org_id=org_id,
                            employee_id=employee_id,
                            tax_year=2026,
                            through_month=2,
                            **values,
                        )
                    )
                    assert registered["status"] == "registered", registered
                    source = session.get(
                        PayrollOpeningState, uuid.UUID(registered["opening_state_id"])
                    )
                corrected = _successor(
                    session, source, **({"version": "corrected-policy"} if kind == "policy" else {})
                )
                session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
            session.commit()
            with owner.attributed_call(session, tool_name="finance_preview_payroll"):
                preview = service.preview_payroll(
                    request.model_copy(
                        update={
                            "idempotency_key": "recomputed-with-corrected-source",
                            "posting_date": date(2026, 3, 31),
                        }
                    )
                )
            assert preview.status == "calculated", preview
            with owner.attributed_call(session, tool_name="finance_confirm_payroll"):
                confirmation = ConfirmPayrollRequest(
                    org_id=org_id,
                    batch_id=preview.batch_id,
                    calculation_hash=preview.calculation_hash,
                    idempotency_key="corrected-confirm",
                )
                posted = service.confirm_payroll(confirmation)
                assert posted.status == "posted", posted
            session.commit()
            with owner.attributed_call(session, tool_name="finance_confirm_payroll"):
                replay = service.confirm_payroll(confirmation)
                assert replay.event_id == posted.event_id and replay.data["idempotent_replay"]
            session.commit()
            with owner.attributed_call(session, tool_name="finance_payroll_source_scope_test"):
                with pytest.raises(
                    DBAPIError, match=f"R6_FINAL_PAYROLL_{kind.upper()}_CORRECTION_BLOCKED"
                ):
                    with session.begin_nested():
                        _successor(
                            session,
                            corrected,
                            **({"version": "forbidden-next-policy"} if kind == "policy" else {}),
                        )
                        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                with pytest.raises(DBAPIError):
                    with session.begin_nested():
                        session.execute(
                            source.__table__.update()
                            .where(source.__table__.c.id == corrected.id)
                            .values(created_at="1900-01-01T00:00:00Z")
                        )
            session.rollback()
