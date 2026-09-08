from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
import sqlalchemy as sa
from _postgres_helpers import authenticated_business_database, confirmed_payroll
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_payroll_service import payroll_parameters

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventComponent,
    EmployeePayrollProfileVersion,
    OpenItem,
    OrganizationDatabaseMetadata,
    PayrollBatch,
    PayrollEventLink,
    PayrollLine,
    PayrollPolicyVersion,
    PayrollTaxStateSlot,
    PayrollWithholdingEntitlement,
    Settlement,
    Voucher,
    VoucherLine,
)
from ai_accounting.schemas import (
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterPayrollPolicyVersionRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@pytest.fixture(scope="module")
def payroll_context() -> Iterator[dict[str, Any]]:
    """One genuine catalog authority confirms the payroll used by every guard test."""
    with authenticated_business_database(
        "finance_company", name="PostgreSQL 工资不变量"
    ) as (engine, org_id, evidence_id, authority):
        with Session(engine) as session:
            _, batch, line, evidence, event = confirmed_payroll(
                session, org_id, evidence_id, authority, key="payroll-invariants"
            )
            session.commit()
            voucher_id = session.scalar(
                sa.select(Voucher.id).where(Voucher.event_id == event.id)
            )
            assert voucher_id is not None
            yield {
                "engine": engine,
                "org_id": org_id,
                "evidence_id": evidence.id,
                "authority": authority,
                "batch_id": batch.id,
                "line_id": line.id,
                "event_id": event.id,
                "voucher_id": voucher_id,
            }


def _post_settlement(
    context: dict[str, Any], *, key: str, categories: set[str]
) -> uuid.UUID:
    with Session(context["engine"]) as session:
        items = list(
            session.scalars(
                sa.select(OpenItem).where(
                    OpenItem.org_id == context["org_id"],
                    OpenItem.payable_category.in_(categories),
                    OpenItem.status.in_({"open", "partial"}),
                )
            )
        )
        assert items and len({item.counterparty_id for item in items}) == 1
        total = sum(item.original_amount_fen - item.settled_amount_fen for item in items)
        if categories == {"salary"}:
            entitlements = list(
                session.scalars(
                    sa.select(PayrollWithholdingEntitlement).where(
                        PayrollWithholdingEntitlement.payroll_line_id
                        == context["line_id"],
                        PayrollWithholdingEntitlement.amount_fen > 0,
                    )
                )
            )
            social = {
                item.insurance_kind: item.amount_fen
                for item in entitlements
                if item.contribution_group == "employee_social_insurance"
            }
            housing = {
                item.insurance_kind: item.amount_fen
                for item in entitlements
                if item.contribution_group == "employee_housing_fund"
            }
            income_tax = sum(
                item.amount_fen
                for item in entitlements
                if item.contribution_group == "individual_income_tax"
            )
            cash_total = total - sum(social.values()) - sum(housing.values()) - income_tax
            component = {
                "key": "settlement",
                "kind": "salary_settlement",
                "business_date": "2026-03-07",
                "payment_date": "2026-03-07",
                "amount_fen": cash_total,
                "allocations": [{"open_item_id": items[0].id, "amount_fen": total}],
                "withholding_allocations": [{
                    "open_item_id": items[0].id,
                    "employee_social_insurance_items": social,
                    "employee_housing_fund_items": housing,
                    "individual_income_tax_fen": income_tax,
                }],
            }
            total = cash_total
        else:
            component = {
                "key": "settlement",
                "kind": "payable_settlement",
                "business_date": "2026-03-07",
                "payment_date": "2026-03-07",
                "counterparty": {"id": items[0].counterparty_id},
                "allocations": [
                    {
                        "open_item_id": item.id,
                        "amount_fen": item.original_amount_fen-item.settled_amount_fen,
                    }
                    for item in items
                ],
            }
        request = RecordEventRequest.model_validate(
            {
                "org_id": context["org_id"],
                "idempotency_key": key,
                "posting_date": "2026-03-07",
                "evidence_references": [context["evidence_id"]],
                "components": [component],
                "funds": [{
                    "key": "cash",
                    "account_code": "1001",
                    "direction": "payment",
                    "payment_date": "2026-03-07",
                    "amount_fen": total,
                    "allocations": [{"component_key": "settlement", "amount_fen": total}],
                }],
            }
        )
        with context["authority"].attributed_call(
            session, tool_name="finance_record_event"
        ):
            result = ComponentService(session).record(request)
        assert result.status == "posted", result.errors
        session.commit()
        assert result.event_id is not None
        return result.event_id


def test_pay_014_final_payroll_batches_and_lines_are_immutable(
    payroll_context: dict[str, Any],
) -> None:
    engine = payroll_context["engine"]
    with Session(engine) as session:
        batch = session.get(PayrollBatch, payroll_context["batch_id"])
        assert batch is not None
        batch.calculation_input = {"tampered": True}
        with pytest.raises(DBAPIError, match="posted payroll batches are immutable"):
            session.flush()
    with Session(engine) as session:
        line = session.get(PayrollLine, payroll_context["line_id"])
        assert line is not None
        line.tax_reported_salary_fen += 1
        with pytest.raises(DBAPIError, match="final payroll lines are immutable"):
            session.flush()
    with Session(engine) as session:
        line = session.get(PayrollLine, payroll_context["line_id"])
        assert line is not None
        session.delete(line)
        with pytest.raises(DBAPIError, match="final payroll lines are immutable"):
            session.flush()


def test_pay_015_organization_links_and_final_shape_are_database_enforced(
    payroll_context: dict[str, Any],
) -> None:
    engine, org_id = payroll_context["engine"], payroll_context["org_id"]
    with Session(engine) as session:
        batch = session.get(PayrollBatch, payroll_context["batch_id"])
        event = session.get(BusinessEvent, payroll_context["event_id"])
        assert batch is not None and event is not None
        links = list(session.scalars(sa.select(PayrollEventLink).where(
            PayrollEventLink.payroll_batch_id == batch.id
        )))
        assert batch.org_id == event.org_id == org_id
        assert batch.status == event.status == "posted"
        assert batch.business_event_id == event.id
        assert {(link.link_kind, link.payroll_batch_id) for link in links} == {
            ("payroll_accrual", batch.id)
        }
        assert all(link.component_id is not None for link in links)
    with Session(engine) as session:
        policy_id = session.scalar(sa.select(PayrollPolicyVersion.id).where(
            PayrollPolicyVersion.org_id == org_id
        ))
        with payroll_context["authority"].attributed_call(
            session, tool_name="finance_negative_final_shape"
        ) as attribution:
            session.add(PayrollBatch(
                org_id=org_id,
                idempotency_key="payroll-incomplete-final",
                batch_kind="regular",
                payroll_period="2026-04",
                version=1,
                status="posted",
                calculation_hash="e" * 64,
                request_payload_hash="f" * 64,
                calculation_input={},
                calculation_trace=[],
                policy_snapshot={},
                policy_version_id=policy_id,
                posting_date=date(2026, 4, 30),
                execution_attribution_id=attribution.id,
            ))
            with pytest.raises(DBAPIError, match="FINAL_PAYROLL_BATCH_COMPONENT_ORIGIN_INVALID"):
                session.commit()


def test_pay_016_final_voucher_lines_reject_insert_update_and_delete(
    payroll_context: dict[str, Any],
) -> None:
    engine, voucher_id = payroll_context["engine"], payroll_context["voucher_id"]
    query = (sa.select(VoucherLine).where(VoucherLine.voucher_id == voucher_id)
             .order_by(VoucherLine.line_number))
    with Session(engine) as session:
        line = session.scalar(query)
        assert line is not None
        line.memo = "tampered"
        with pytest.raises(DBAPIError, match="final voucher"):
            session.flush()
    with Session(engine) as session:
        line = session.scalar(query)
        assert line is not None
        session.delete(line)
        with pytest.raises(DBAPIError, match="final voucher"):
            session.flush()
    with Session(engine) as session:
        line = session.scalar(query)
        assert line is not None
        session.add(VoucherLine(
            org_id=line.org_id,
            voucher_id=line.voucher_id,
            component_id=line.component_id,
            line_number=99,
            account_id=line.account_id,
            debit_fen=1,
        ))
        with pytest.raises(DBAPIError, match="final voucher"):
            session.flush()


def test_pay_017_open_item_settlement_conservation_and_org_links(
    payroll_context: dict[str, Any],
) -> None:
    engine = payroll_context["engine"]
    item_query = sa.select(OpenItem).where(
        OpenItem.org_id == payroll_context["org_id"],
        OpenItem.payable_category == "salary",
    )
    with Session(engine) as session:
        item = session.scalar(item_query)
        assert item is not None
        item.settled_amount_fen = 1
        item.status = "partial"
        with pytest.raises(DBAPIError, match="settlement total"):
            session.commit()
    with Session(engine) as session:
        item = session.scalar(item_query)
        assert item is not None
        session.add(Settlement(
            org_id=uuid.uuid4(),
            open_item_id=item.id,
            payment_event_id=payroll_context["event_id"],
            amount_fen=1,
        ))
        with pytest.raises(IntegrityError):
            session.flush()


def test_r2_003_per_insurance_withholding_cannot_be_reallocated(
    payroll_context: dict[str, Any],
) -> None:
    payroll_context["salary_event_id"] = _post_settlement(
        payroll_context,
        key="payroll-salary-payment",
        categories={"salary"},
    )
    payment_event_id = _post_settlement(
        payroll_context,
        key="payroll-statutory-payment",
        categories={"employer_social", "withheld_employee_social"},
    )
    payroll_context["statutory_event_id"] = payment_event_id
    with Session(payroll_context["engine"]) as session:
        settlements = list(
            session.scalars(
                sa.select(Settlement).where(Settlement.payment_event_id == payment_event_id)
            )
        )
        assert settlements
        items = [session.get(OpenItem, settlement.open_item_id) for settlement in settlements]
        assert all(item is not None for item in items)
        assert {item.payable_category for item in items} == {
            "employer_social",
            "withheld_employee_social",
        }
        assert all(item.insurance_kind for item in items)
        assert all(item.status == "settled" for item in items)
        settlements[0].open_item_id = settlements[1].open_item_id
        with pytest.raises(IntegrityError, match="uq_settlement_component_item"):
            session.flush()


def test_r3_003_posted_withholding_entitlements_and_allocations_are_append_only(
    payroll_context: dict[str, Any],
) -> None:
    engine = payroll_context["engine"]
    entitlement_query = sa.select(PayrollWithholdingEntitlement).where(
        PayrollWithholdingEntitlement.payroll_line_id == payroll_context["line_id"],
        PayrollWithholdingEntitlement.amount_fen > 0,
    )
    with Session(engine) as session:
        entitlement = session.scalar(entitlement_query)
        assert entitlement is not None
        entitlement.amount_fen += 1
        with pytest.raises(DBAPIError, match="entitlements are immutable"):
            session.flush()
    with Session(engine) as session:
        entitlement = session.scalar(entitlement_query)
        assert entitlement is not None
        session.delete(entitlement)
        with pytest.raises(DBAPIError, match="entitlements are immutable"):
            session.flush()
    with Session(engine) as session:
        settlement = session.scalar(
            sa.select(Settlement).where(
                Settlement.payment_event_id != payroll_context["event_id"]
            )
        )
        assert settlement is not None
        settlement.amount_fen += 1
        with pytest.raises(DBAPIError, match="COMPONENT_SETTLEMENT_ENTRY_MISMATCH"):
            session.commit()


def test_r3_004_final_event_state_requires_draft_and_keeps_refund_original_posted(
    payroll_context: dict[str, Any],
) -> None:
    engine, authority = payroll_context["engine"], payroll_context["authority"]
    with Session(engine) as session:
        with authority.attributed_call(
            session, tool_name="finance_negative_event_state"
        ) as attribution:
            session.add(BusinessEvent(
                org_id=payroll_context["org_id"],
                idempotency_key="direct-reversed-event",
                event_type="reversal",
                status="reversed",
                description="invalid final insert",
                facts={},
                business_date=date(2026, 3, 1),
                posting_date=date(2026, 3, 1),
                rule_trace=[],
                execution_attribution_id=attribution.id,
            ))
            with pytest.raises(DBAPIError, match="created as draft"):
                session.flush()
    salary_event_id = payroll_context["salary_event_id"]
    with Session(engine) as session:
        with authority.attributed_call(session, tool_name="finance_reverse_event"):
            statutory_reversal = FinanceService(session).reverse_event(ReverseEventRequest(
                org_id=payroll_context["org_id"],
                event_id=payroll_context["statutory_event_id"],
                idempotency_key="payroll-statutory-payment-reversal",
                reason="reverse typed statutory settlement before its salary source",
                posting_date=date(2026, 3, 8),
            ))
        assert statutory_reversal.status == "posted", statutory_reversal.errors
        session.commit()
    with Session(engine) as session:
        with authority.attributed_call(session, tool_name="finance_reverse_event"):
            reversal = FinanceService(session).reverse_event(ReverseEventRequest(
                org_id=payroll_context["org_id"],
                event_id=salary_event_id,
                idempotency_key="payroll-salary-payment-reversal",
                reason="reverse typed salary settlement",
                posting_date=date(2026, 3, 8),
            ))
        assert reversal.status == "posted", reversal.errors
        session.commit()
        accrual = session.get(BusinessEvent, payroll_context["event_id"])
        payment = session.get(BusinessEvent, salary_event_id)
        assert accrual is not None and accrual.status == "posted"
        assert payment is not None and payment.status == "reversed"


def test_r3_002_tax_state_slot_rejects_cross_employee_and_arbitrary_mutation(
    payroll_context: dict[str, Any],
) -> None:
    engine, authority = payroll_context["engine"], payroll_context["authority"]
    org_id = payroll_context["org_id"]
    with Session(engine) as session:
        with authority.attributed_call(session, tool_name="finance_register_employee"):
            second = FinanceService(session).register_employee(RegisterEmployeeRequest(
                org_id=org_id,
                employee_code="payroll-tax-slot-second",
                name="第二名员工",
                employment_start_date=date(2026, 3, 1),
                tax_withholding_start_date=date(2026, 3, 1),
                status="active",
            ))
        assert second["status"] == "registered"
        second_id = uuid.UUID(second["employee_id"])
        session.commit()
    with Session(engine) as session:
        session.add(PayrollTaxStateSlot(
            org_id=org_id,
            employee_id=second_id,
            tax_year=2026,
            tax_month=8,
            regular_batch_id=payroll_context["batch_id"],
            final_batch_id=payroll_context["batch_id"],
        ))
        with pytest.raises(DBAPIError, match="same-employee regular payroll"):
            session.commit()
    with Session(engine) as session:
        slot = session.scalar(sa.select(PayrollTaxStateSlot).where(
            PayrollTaxStateSlot.regular_batch_id == payroll_context["batch_id"]
        ))
        assert slot is not None
        slot.tax_month = 8
        with pytest.raises(DBAPIError, match="identity and regular batch are immutable"):
            session.flush()


def test_r2_005_final_vouchers_and_business_events_are_database_immutable(
    payroll_context: dict[str, Any],
) -> None:
    with Session(payroll_context["engine"]) as session:
        event = session.get(BusinessEvent, payroll_context["event_id"])
        assert event is not None
        event.facts = {"tampered": True}
        with pytest.raises(DBAPIError, match="final business events are immutable"):
            session.flush()
    with Session(payroll_context["engine"]) as session:
        voucher = session.get(Voucher, payroll_context["voucher_id"])
        assert voucher is not None
        voucher.description = "tampered"
        with pytest.raises(DBAPIError, match="final voucher"):
            session.flush()


def test_r2_006_voucher_line_composite_organization_foreign_keys(
    payroll_context: dict[str, Any],
) -> None:
    with Session(payroll_context["engine"]) as session:
        binding = session.get(OrganizationDatabaseMetadata, 1)
        component = session.scalar(sa.select(BusinessEventComponent).where(
            BusinessEventComponent.event_id == payroll_context["event_id"]
        ))
        line = session.scalar(sa.select(VoucherLine).where(
            VoucherLine.voucher_id == payroll_context["voucher_id"]
        ))
        assert binding is not None and binding.org_id == payroll_context["org_id"]
        assert component is not None and line is not None
        assert component.org_id == line.org_id == payroll_context["org_id"]
        assert line.component_id == component.id
        with payroll_context["authority"].attributed_call(
            session, tool_name="finance_negative_voucher_org_link"
        ) as attribution:
            draft_event = BusinessEvent(
                org_id=payroll_context["org_id"],
                idempotency_key="payroll-draft-org-link",
                event_type="composite",
                status="draft",
                description="negative organization-link fixture",
                facts={},
                business_date=date(2026, 3, 9),
                posting_date=date(2026, 3, 9),
                rule_trace=[],
                execution_attribution_id=attribution.id,
            )
            session.add(draft_event)
            session.flush()
            draft_voucher = Voucher(
                org_id=payroll_context["org_id"],
                event_id=draft_event.id,
                voucher_number="202603-org-link-draft",
                posting_date=date(2026, 3, 9),
                description="negative organization-link fixture",
                status="draft",
            )
            session.add(draft_voucher)
            session.flush()
        session.add(VoucherLine(
            org_id=uuid.uuid4(),
            voucher_id=draft_voucher.id,
            line_number=100,
            account_id=line.account_id,
            debit_fen=1,
        ))
        with pytest.raises(IntegrityError):
            session.flush()


def test_r2_010_explicit_version_successors_allow_only_their_own_overlap(
    payroll_context: dict[str, Any],
) -> None:
    engine, authority = payroll_context["engine"], payroll_context["authority"]
    org_id = payroll_context["org_id"]
    with Session(engine) as session:
        service = FinanceService(session)
        with authority.attributed_call(session, tool_name="finance_register_employee"):
            employee = service.register_employee(RegisterEmployeeRequest(
                org_id=org_id,
                employee_code="payroll-version-successor",
                name="版本继任测试员工",
                employment_start_date=date(2026, 4, 1),
                tax_withholding_start_date=date(2026, 4, 1),
                status="active",
            ))
        assert employee["status"] == "registered", employee
        employee_id = uuid.UUID(employee["employee_id"])
        with authority.attributed_call(
            session, tool_name="finance_register_employee_payroll_profile_version"
        ):
            profile_root = service.register_employee_payroll_profile_version(
                RegisterEmployeePayrollProfileVersionRequest(
                    org_id=org_id,
                    employee_id=employee_id,
                    effective_from=date(2026, 4, 1),
                    expense_role="payroll_management_expense",
                    social_insurance_base_fen=500_000,
                    housing_fund_base_fen=500_000,
                    resident_employee=True,
                )
            )
        assert profile_root["status"] == "registered", profile_root
        profile_id = uuid.UUID(profile_root["profile_version_id"])
        with authority.attributed_call(
            session, tool_name="finance_register_payroll_policy_version"
        ):
            policy_root = service.register_payroll_policy_version(
                RegisterPayrollPolicyVersionRequest(
                    org_id=org_id,
                    region="payroll-version-region",
                    effective_from=date(2026, 4, 1),
                    version="payroll-version-root",
                    source_url="https://www.chinatax.gov.cn/",
                    parameters=payroll_parameters(),
                )
            )
        assert policy_root["status"] == "registered", policy_root
        policy_id = uuid.UUID(policy_root["policy_version_id"])
        with authority.attributed_call(
            session, tool_name="finance_register_employee_payroll_profile_version"
        ):
            profile_successor = service.register_employee_payroll_profile_version(
                RegisterEmployeePayrollProfileVersionRequest(
                    org_id=org_id,
                    employee_id=employee_id,
                    effective_from=date(2026, 4, 1),
                    expense_role="payroll_management_expense",
                    social_insurance_base_fen=500_000,
                    housing_fund_base_fen=500_000,
                    resident_employee=True,
                    supersedes_profile_version_id=profile_id,
                )
            )
        assert profile_successor["status"] == "registered", profile_successor
        with authority.attributed_call(
            session, tool_name="finance_register_payroll_policy_version"
        ):
            policy_successor = service.register_payroll_policy_version(
                RegisterPayrollPolicyVersionRequest(
                    org_id=org_id,
                    region="payroll-version-region",
                    effective_from=date(2026, 4, 1),
                    version="payroll-version-successor",
                    source_url="https://www.chinatax.gov.cn/",
                    parameters=payroll_parameters(),
                    supersedes_policy_version_id=policy_id,
                )
            )
        assert policy_successor["status"] == "registered", policy_successor
        session.commit()
    with Session(engine) as session:
        profile = session.get(EmployeePayrollProfileVersion, profile_id)
        assert profile is not None
        with authority.attributed_call(
            session, tool_name="finance_negative_profile_overlap"
        ) as attribution:
            session.add(EmployeePayrollProfileVersion(
                org_id=org_id,
                employee_id=employee_id,
                effective_from=profile.effective_from,
                expense_role=profile.expense_role,
                social_insurance_base_fen=profile.social_insurance_base_fen,
                housing_fund_base_fen=profile.housing_fund_base_fen,
                social_insurance_participating=True,
                housing_fund_participating=True,
                resident_employee=True,
                execution_attribution_id=attribution.id,
            ))
            with pytest.raises(
                DBAPIError, match="NON_ANCESTOR_OVERLAP|explicit supersession"
            ):
                session.commit()
