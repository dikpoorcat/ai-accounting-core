from datetime import date

import pytest
from _postgres_helpers import authenticated_business_database
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_confirmed_accrual_components import _calculated_batches

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.labor_remuneration_schemas import (
    LaborRemunerationItemFacts,
    PreviewLaborRemunerationBatchRequest,
)
from ai_accounting.labor_remuneration_service import LaborRemunerationService
from ai_accounting.models import (
    BusinessEventComponent,
    Evidence,
    LaborRemunerationEventLink,
    LaborRemunerationLine,
    OpenItem,
    Organization,
    Settlement,
    Voucher,
    VoucherLine,
)


def _labor_accrual(key, batch, evidence_id):
    return {
        "key": key,
        "kind": "labor_remuneration_accrual",
        "business_date": "2026-03-05",
        "batch_id": batch.batch_id,
        "calculation_hash": batch.calculation_hash,
        "confirmation_note": f"确认重复劳务批次 {key}",
        "evidence_references": [evidence_id],
    }


def _labor_settlement(key, source, line):
    facts = {
        "key": key,
        "kind": "labor_settlement",
        "business_date": "2026-03-05",
        "payment_date": "2026-03-05",
        "source_open_item_key": str(line.id),
        "amount_fen": line.gross_remuneration_fen,
        "settlement_mode": "net_after_withholding",
        "withholding_agency_code": "TAX-LABOR-REPEATED",
        "withholding_agency_name": "重复劳务组件测试税务局",
    }
    if isinstance(source, str):
        facts["source_component_key"] = source
    else:
        facts["source_open_item_id"] = source
    return facts


@pytest.mark.postgres
@pytest.mark.parametrize("same_event", [True, False], ids=["local", "later"])
def test_repeated_labor_components_keep_exact_source_ownership(same_event):
    prefix = f"repeated_labor_{'local' if same_event else 'later'}"
    with authenticated_business_database(prefix) as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            evidence = session.get(Evidence, evidence_id)
            with authority.attributed_call(session, tool_name="finance_record_event"):
                _, _, first_batch, _ = _calculated_batches(session, organization, evidence)
                first_line = session.scalar(
                    select(LaborRemunerationLine).where(
                        LaborRemunerationLine.batch_id == first_batch.batch_id
                    )
                )
                second_batch = LaborRemunerationService(session).preview_batch(
                    PreviewLaborRemunerationBatchRequest(
                        org_id=org_id,
                        idempotency_key="component-labor-preview-second",
                        remuneration_period="2026-03",
                        business_date=date(2026, 3, 5),
                        posting_date=date(2026, 3, 5),
                        planned_payment_date=date(2026, 3, 5),
                        items=[
                            LaborRemunerationItemFacts(
                                labor_person_id=first_line.labor_person_id,
                                service_start_date=date(2026, 3, 1),
                                service_end_date=date(2026, 3, 5),
                                fixed_fee_fen=100_000,
                                commission_fen=0,
                                expense_role="labor_management_expense",
                                tax_identity="resident",
                                income_grouping="single_occurrence",
                                is_full_time_student=False,
                                external_declaration_status="not_due",
                            )
                        ],
                        evidence_references=[evidence_id],
                    )
                )
                assert second_batch.status == "calculated", second_batch
                second_line = session.scalar(
                    select(LaborRemunerationLine).where(
                        LaborRemunerationLine.batch_id == second_batch.batch_id
                    )
                )
                batches = {"labor-one": first_batch, "labor-two": second_batch}
                lines = {"labor-one": first_line, "labor-two": second_line}
                accruals = [
                    _labor_accrual(key, batch, evidence_id)
                    for key, batch in batches.items()
                ]

                if same_event:
                    settlement_sources = {key: key for key in batches}
                    components = [
                        *accruals,
                        *[
                            _labor_settlement(f"pay-{key}", settlement_sources[key], lines[key])
                            for key in batches
                        ],
                    ]
                    accrual_event = payment_event = ComponentService(session).record(
                        RecordEventRequest.model_validate(
                            {
                                "org_id": org_id,
                                "idempotency_key": "repeated-labor-local-settlements",
                                "posting_date": "2026-03-05",
                                "components": components,
                                "funds": [_cash_funds(lines, batches)],
                            }
                        )
                    )
                    assert payment_event.status == "posted", payment_event
                else:
                    accrual_event = ComponentService(session).record(
                        RecordEventRequest.model_validate(
                            {
                                "org_id": org_id,
                                "idempotency_key": "repeated-labor-accruals",
                                "posting_date": "2026-03-05",
                                "components": accruals,
                            }
                        )
                    )
                    assert accrual_event.status == "posted", accrual_event
                    accrual_components = _components(session, accrual_event.event_id)
                    source_items = {
                        key: session.scalar(
                            select(OpenItem).where(
                                OpenItem.source_component_id == accrual_components[key].id,
                                OpenItem.component_key == str(lines[key].id),
                            )
                        )
                        for key in batches
                    }
                    payment_event = ComponentService(session).record(
                        RecordEventRequest.model_validate(
                            {
                                "org_id": org_id,
                                "idempotency_key": "repeated-labor-later-settlements",
                                "posting_date": "2026-03-05",
                                "evidence_references": [evidence_id],
                                "components": [
                                    _labor_settlement(
                                        f"pay-{key}", source_items[key].id, lines[key]
                                    )
                                    for key in batches
                                ],
                                "funds": [_cash_funds(lines, batches)],
                            }
                        )
                    )
                    assert payment_event.status == "posted", payment_event
                session.commit()

            accrual_components = _components(session, accrual_event.event_id)
            payment_components = _components(session, payment_event.event_id)
            for key, batch in batches.items():
                line = lines[key]
                source_item = session.scalar(
                    select(OpenItem).where(
                        OpenItem.source_component_id == accrual_components[key].id,
                        OpenItem.component_key == str(line.id),
                    )
                )
                payment_link = session.scalar(
                    select(LaborRemunerationEventLink).where(
                        LaborRemunerationEventLink.event_id == payment_event.event_id,
                        LaborRemunerationEventLink.component_id
                        == payment_components[f"pay-{key}"].id,
                        LaborRemunerationEventLink.link_kind == "payment",
                    )
                )
                settlement = session.scalar(
                    select(Settlement).where(
                        Settlement.payment_component_id
                        == payment_components[f"pay-{key}"].id
                    )
                )
                assert source_item.status == "settled"
                assert source_item.settled_amount_fen == line.gross_remuneration_fen
                assert payment_link.batch_id == batch.batch_id
                assert payment_link.labor_line_id == line.id
                assert payment_link.source_open_item_id == source_item.id
                assert settlement.open_item_id == source_item.id
                assert settlement.amount_fen == line.gross_remuneration_fen

            for event_id in {accrual_event.event_id, payment_event.event_id}:
                voucher = session.scalar(select(Voucher).where(Voucher.event_id == event_id))
                voucher_lines = session.scalars(
                    select(VoucherLine).where(VoucherLine.voucher_id == voucher.id)
                ).all()
                assert sum(line.debit_fen for line in voucher_lines) == sum(
                    line.credit_fen for line in voucher_lines
                )


def _cash_funds(lines, batches):
    amount = sum(lines[key].net_payment_fen for key in batches)
    return {
        "key": "cash",
        "account_code": "1001",
        "direction": "payment",
        "payment_date": "2026-03-05",
        "amount_fen": amount,
        "allocations": [
            {"component_key": f"pay-{key}", "amount_fen": lines[key].net_payment_fen}
            for key in batches
        ],
    }


def _components(session, event_id):
    return {
        component.key: component
        for component in session.scalars(
            select(BusinessEventComponent).where(BusinessEventComponent.event_id == event_id)
        )
    }
