from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from _postgres_helpers import authenticated_business_database
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_component_guards_postgres import component_database as _database_fixture
from test_purchase_components import (
    PARTY,
    advance,
    asset,
    component_id,
    facts,
    item,
    payment,
    record,
    request,
    stage,
)
from test_purchase_components import (
    test_advance_cross_month_asset_application_tail_and_reversal as _advance_case,
)
from test_purchase_components import (
    test_development_conditions_and_mixed_cost_breakdown as _development_case,
)
from test_purchase_components import (
    test_partial_refund_amend_delete_restores_exact_balance as _refund_case,
)
from test_purchase_components import (
    test_stage_payables_paid_then_asset_consumes_cost_without_new_debt as _stage_case,
)

from ai_accounting import replay_cli
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.event_amendment_schemas import DeleteEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.models import Evidence, Organization

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]
component_database = _database_fixture


@pytest.mark.parametrize("case", [_advance_case, _stage_case, _refund_case, _development_case])
def test_purchase_lifecycles_on_postgres_17(component_database, case, monkeypatch):
    engine, (org_id, evidence_id, authority) = component_database
    errors = []

    def diagnostic(context):
        errors.append(str(context.original_exception))

    event.listen(engine, "handle_error", diagnostic)
    original_amend = EventAmendmentService.amend

    def attributed_amend(service, request):
        tool = (
            "finance_delete_event"
            if isinstance(request, DeleteEventRequest)
            else "finance_amend_event"
        )
        with authority.attributed_call(service.session, tool_name=tool):
            return original_amend(service, request)

    monkeypatch.setattr(EventAmendmentService, "amend", attributed_amend)
    try:
        with Session(engine) as session:
            organization = session.scalar(select(Organization).where(Organization.id == org_id))
            with authority.attributed_call(session, tool_name="finance_record_event"):
                try:
                    case(session, organization, SimpleNamespace(id=evidence_id))
                except AssertionError as exc:
                    raise AssertionError(f"{exc}; database diagnostics: {errors}") from exc
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            session.rollback()
    finally:
        event.remove(engine, "handle_error", diagnostic)


def test_database_rejects_balanced_stage_posted_as_expense(component_database, monkeypatch):
    engine, (org_id, evidence_id, authority) = component_database
    original = ComponentService.compile_project_cost

    def wrong_account(service, c):
        plan = original(service, c)
        plan.entries[0] = replace(plan.entries[0], account_role="general_expense")
        return plan

    monkeypatch.setattr(ComponentService, "compile_project_cost", wrong_account)
    with Session(engine) as session:
        with authority.attributed_call(session, tool_name="finance_record_event"):
            record(
                session,
                request(
                    session.get(Organization, org_id),
                    SimpleNamespace(id=evidence_id),
                    "2022-09-21",
                    "bad-cost",
                    [stage()],
                ),
            )
        with pytest.raises(DBAPIError, match="PURCHASE_COMPONENT_ENTRY_MISMATCH"):
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        session.rollback()


def test_database_rejects_overallocation_even_if_compiler_balance_check_is_bypassed(
    component_database, monkeypatch
):
    from ai_accounting import purchase_components

    engine, (org_id, evidence_id, authority) = component_database
    with Session(engine) as session:
        org, evidence = session.get(Organization, org_id), SimpleNamespace(id=evidence_id)
        with authority.attributed_call(session, tool_name="finance_record_event"):
            first = record(session, request(org, evidence, "2022-09-21", "stage", [stage()]))
            source = {"component_id": component_id(first, "stage"), "amount_fen": 500000}
            record(
                session,
                request(
                    org,
                    evidence,
                    "2022-11-30",
                    "asset-one",
                    [asset("project_cost", [source], amount=500000)],
                ),
            )
            monkeypatch.setattr(purchase_components, "_source_identity", lambda *args: object())
            record(
                session,
                request(
                    org,
                    evidence,
                    "2022-11-30",
                    "asset-two",
                    [
                        asset(
                            "project_cost",
                            [{**source, "amount_fen": 400000}],
                            code="UI-SECOND",
                            amount=400000,
                        )
                    ],
                ),
            )
        with pytest.raises(DBAPIError, match="PROJECT_COST_SOURCE_AMOUNT_EXCEEDED"):
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        session.rollback()


def test_purchase_sources_replay_with_new_ids_in_empty_postgres(monkeypatch):
    with (
        authenticated_business_database("purchase_replay") as source,
        authenticated_business_database("purchase_replay") as target,
    ):
        source_engine, source_org, source_evidence, source_auth = source
        target_engine, target_org, target_evidence, target_auth = target
        with Session(source_engine) as session:
            org, evidence = (
                session.get(Organization, source_org),
                session.get(Evidence, source_evidence),
            )
            with source_auth.attributed_call(session, tool_name="finance_record_event"):
                prepaid = record(
                    session,
                    request(
                        org, evidence, "2022-09-21", "advance", [advance()], [("advance", 800000)]
                    ),
                )
                accepted = record(
                    session,
                    request(
                        org,
                        evidence,
                        "2022-09-21",
                        "stage",
                        [
                            stage(),
                            payment("pay", "2022-09-21", {"source_component_key": "stage"}, 800000),
                        ],
                        [("pay", 800000)],
                    ),
                )
                record(
                    session,
                    request(
                        org,
                        evidence,
                        "2022-11-30",
                        "delivery",
                        [
                            stage("last", "2022-11-30"),
                            asset(
                                "project_cost",
                                [
                                    {
                                        "component_id": component_id(accepted, "stage"),
                                        "amount_fen": 800000,
                                    },
                                    {"component_key": "last", "amount_fen": 800000},
                                ],
                            ),
                            facts(
                                "supplier_advance_application",
                                "apply",
                                "2022-11-30",
                                advances=[
                                    {
                                        "open_item_id": item(
                                            session, prepaid.event_id, "receivable"
                                        ).id,
                                        "amount_fen": 800000,
                                    }
                                ],
                                allocations=[
                                    {"source_component_key": "last", "amount_fen": 800000}
                                ],
                                metadata={"counterparty": PARTY},
                            ),
                        ],
                    ),
                )
            session.commit()
            maps = replay_cli._stable_maps(session, source_org)
            events = replay_cli._effective_events(session, source_org)
            operations = [
                replay_cli._event_operation(session, event, org_id=source_org, maps=maps)
                for event in events
            ]
        with Session(target_engine) as session:

            def call(tool_name, raw):
                with target_auth.attributed_call(session, tool_name=tool_name):
                    service = ComponentService(session)
                    typed = RecordEventRequest.model_validate(raw)
                    result = (
                        service.preview(typed)
                        if tool_name == "finance_preview_event"
                        else service.record(typed)
                    )
                if tool_name == "finance_preview_event":
                    session.rollback()
                else:
                    session.commit()
                return result.model_dump(mode="json")

            monkeypatch.setattr(replay_cli, "_call_tool", call)
            resolver = replay_cli._ReplayResolver(
                engine=target_engine, org_id=target_org, results={}
            )
            for operation in operations:
                replayed = replay_cli._execute_operation(
                    operation, package_company_dir=Path.cwd(), resolver=resolver
                )
                assert replayed["status"] == "posted", replayed
                resolver.results[operation["key"]] = replayed
        with Session(source_engine) as original, Session(target_engine) as restored:
            assert replay_cli._account_balance_projection(
                original, source_org
            ) == replay_cli._account_balance_projection(restored, target_org)
            assert replay_cli._open_item_projection(
                original, org_id=source_org
            ) == replay_cli._open_item_projection(restored, org_id=target_org)
