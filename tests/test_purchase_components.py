from copy import deepcopy

import pytest
from sqlalchemy import func, select
from test_business_components import sample_evidence as _evidence_fixture

from ai_accounting.component_schemas import ConfigureAccountRequest, RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.event_amendment_schemas import AmendEventRequest, DeleteEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.models import BusinessEvent, IntangibleAsset, OpenItem, VoucherLine
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService

sample_evidence = _evidence_fixture
PARTY = {"kind": "supplier", "name": "Project supplier"}
PROJECT = "contract-ui-2022"


def facts(kind, key, day, **values):
    return {"kind": kind, "key": key, "business_date": day, "project_reference": PROJECT, **values}


def advance(day="2022-09-21", amount=800000, **values):
    return facts(
        "supplier_advance",
        "advance",
        day,
        payment_date=day,
        counterparty=PARTY,
        amount_fen=amount,
        purchase_purpose="intangible_asset",
        contract_reference="signed-contract-2022",
        **values,
    )


def stage(key="stage", day="2022-09-21", amount=800000, **values):
    return facts(
        "project_cost",
        key,
        day,
        amount_fen=amount,
        counterparty=PARTY,
        project_nature="purchased_intangible",
        cost_element="purchase_price",
        acceptance_reference=f"acceptance-{key}",
        obligation_reference=f"invoice-{key}",
        rights_controlled=True,
        capitalization_basis="合同阶段成果已验收，权利已取得，直接构成软件成本",
        due_date=day,
        **values,
    )


def asset(method="payable", sources=None, code="UI-2022", amount=1600000):
    return facts(
        "intangible_asset_acquisition",
        "asset",
        "2022-11-30",
        cost_sources=sources or [],
        facts={
            "asset_code": code,
            "asset_name": "UI design rights",
            "category": "software",
            "rights_description": "永久独占使用权",
            "supplier": PARTY,
            "available_for_use_date": "2022-11-30",
            "cost_components": {
                "purchase_price_fen": amount,
                "noncreditable_tax_fen": 0,
                "directly_attributable_cost_fen": 0,
            },
            "settlement_method": method,
            "due_date": "2022-11-30" if method == "payable" else None,
            "benefit_area": "management",
            "life_basis": "reliably_estimated",
            "useful_life_months": 60,
            "life_basis_explanation": "预计使用五年",
            "is_available_for_use": True,
            "claims_creditable_input_vat": False,
        },
    )


def request(org, evidence, day, key, components, payments=None, *, receipt=False):
    payments = payments or []
    return RecordEventRequest.model_validate(
        {
            "org_id": org.id,
            "idempotency_key": key,
            "posting_date": day,
            "evidence_references": [evidence.id],
            "components": components,
            "funds": [
                {
                    "key": "money",
                    "account_code": "1001",
                    "direction": "receipt" if receipt else "payment",
                    "payment_date": day,
                    "amount_fen": sum(a for _, a in payments),
                    "allocations": [{"component_key": k, "amount_fen": a} for k, a in payments],
                }
            ]
            if payments
            else [],
        }
    )


def payment(key, day, source, amount):
    return facts(
        "payable_settlement",
        key,
        day,
        counterparty=PARTY,
        payment_date=day,
        allocations=[{**source, "amount_fen": amount}],
    )


def record(session, req):
    result = ComponentService(session).record(req)
    assert result.status == "posted", result
    return result


def item(session, event_id, kind):
    return session.scalar(
        select(OpenItem).where(OpenItem.source_event_id == event_id, OpenItem.item_type == kind)
    )


def component_id(result, key):
    return next(c["id"] for c in result.data["components"] if c["key"] == key)


def test_advance_cross_month_asset_application_tail_and_reversal(
    session, organization, sample_evidence
):
    first_req = request(
        organization, sample_evidence, "2022-09-21", "advance", [advance()], [("advance", 800000)]
    )
    first = record(session, first_req)
    assert record(session, first_req).event_id == first.event_id
    prepaid = item(session, first.event_id, "receivable")
    apply = facts(
        "supplier_advance_application",
        "apply",
        "2022-11-30",
        counterparty=PARTY,
        advances=[{"open_item_id": prepaid.id, "amount_fen": 800000}],
        allocations=[{"source_component_key": "asset", "amount_fen": 800000}],
    )
    final_req = request(
        organization,
        sample_evidence,
        "2022-11-30",
        "complete",
        [apply, payment("tail", "2022-11-30", {"source_component_key": "asset"}, 800000), asset()],
        [("tail", 800000)],
    )
    final = record(session, final_req)
    assert prepaid.settled_amount_fen == 800000
    assert item(session, final.event_id, "payable").settled_amount_fen == 1600000
    assert session.scalar(select(IntangibleAsset)).cost_fen == 1600000
    reversed_result = FinanceService(session).reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=final.event_id,
            posting_date="2022-12-01",
            idempotency_key="reverse-completion",
            reason="项目整体更正",
        )
    )
    assert reversed_result.status == "posted", reversed_result
    assert prepaid.settled_amount_fen == 0


def test_stage_payables_paid_then_asset_consumes_cost_without_new_debt(
    session, organization, sample_evidence
):
    first = record(
        session,
        request(
            organization,
            sample_evidence,
            "2022-09-21",
            "stage-one",
            [stage(), payment("pay", "2022-09-21", {"source_component_key": "stage"}, 800000)],
            [("pay", 800000)],
        ),
    )
    completed = record(
        session,
        request(
            organization,
            sample_evidence,
            "2022-11-30",
            "stage-two",
            [
                asset(
                    "project_cost",
                    [
                        {"component_id": component_id(first, "stage"), "amount_fen": 800000},
                        {"component_key": "last", "amount_fen": 800000},
                    ],
                ),
                stage("last", "2022-11-30"),
                payment("pay", "2022-11-30", {"source_component_key": "last"}, 800000),
            ],
            [("pay", 800000)],
        ),
    )
    assert len(completed.data["created_open_items"]) == 1
    assert session.scalar(select(IntangibleAsset)).settlement_method == "project_cost"
    assert session.scalar(select(func.sum(OpenItem.original_amount_fen))) == 1600000
    refused = FinanceService(session).reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=first.event_id,
            posting_date="2022-12-01",
            idempotency_key="reverse-parent",
            reason="不能破坏资产来源",
        )
    )
    assert refused.status == "rejected", refused
    duplicate = ComponentService(session).record(
        request(
            organization,
            sample_evidence,
            "2022-11-30",
            "double-cost",
            [
                asset(
                    "project_cost",
                    [{"component_id": component_id(first, "stage"), "amount_fen": 800000}],
                    code="UI-DUP",
                    amount=800000,
                )
            ],
        )
    )
    assert duplicate.errors == ["PROJECT_COST_SOURCE_AMOUNT_EXCEEDED"]


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"project_reference": "wrong"}, "PURCHASE_SETTLEMENT_PROJECT_MISMATCH"),
        (
            {"counterparty": {"kind": "supplier", "name": "Wrong supplier"}},
            "PURCHASE_SETTLEMENT_SUPPLIER_OR_DIRECTION_MISMATCH",
        ),
    ],
)
def test_refund_source_scope_and_atomicity(
    session, organization, sample_evidence, change, expected
):
    first = record(
        session,
        request(
            organization,
            sample_evidence,
            "2022-09-21",
            "advance",
            [advance()],
            [("advance", 800000)],
        ),
    )
    prepaid = item(session, first.event_id, "receivable")
    refund = facts(
        "supplier_advance_refund",
        "refund",
        "2022-09-22",
        counterparty=PARTY,
        payment_date="2022-09-22",
        refund_reference="cancellation",
        advances=[{"open_item_id": prepaid.id, "amount_fen": 200000}],
    )
    refund.update(change)
    rejected = ComponentService(session).record(
        request(
            organization,
            sample_evidence,
            "2022-09-22",
            "bad-refund",
            [refund],
            [("refund", 200000)],
            receipt=True,
        )
    )
    assert rejected.errors == [expected]
    assert prepaid.settled_amount_fen == 0
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 1


def test_partial_refund_amend_delete_restores_exact_balance(session, organization, sample_evidence):
    first = record(
        session,
        request(
            organization,
            sample_evidence,
            "2022-09-21",
            "advance",
            [advance()],
            [("advance", 800000)],
        ),
    )
    prepaid = item(session, first.event_id, "receivable")
    refund = facts(
        "supplier_advance_refund",
        "refund",
        "2022-09-22",
        counterparty=PARTY,
        payment_date="2022-09-22",
        refund_reference="partial-cancellation",
        advances=[{"open_item_id": prepaid.id, "amount_fen": 200000}],
    )
    posted = record(
        session,
        request(
            organization,
            sample_evidence,
            "2022-09-22",
            "refund",
            [refund],
            [("refund", 200000)],
            receipt=True,
        ),
    )
    replacement = deepcopy(refund)
    replacement["advances"][0]["amount_fen"] = 300000
    amended = EventAmendmentService(session).amend(
        AmendEventRequest(
            org_id=organization.id,
            event_id=posted.event_id,
            idempotency_key="amend-refund",
            expected_facts_hash=posted.data["facts_hash"],
            reason="更正退款额",
            replacement=request(
                organization,
                sample_evidence,
                "2022-09-22",
                "refund",
                [replacement],
                [("refund", 300000)],
                receipt=True,
            ),
        )
    )
    assert amended["status"] == "posted", amended
    assert prepaid.settled_amount_fen == 300000
    deleted = EventAmendmentService(session).amend(
        DeleteEventRequest(
            org_id=organization.id,
            event_id=posted.event_id,
            idempotency_key="delete-refund",
            reason="撤销退款",
            expected_facts_hash=amended["data"]["facts_hash"],
        )
    )
    assert deleted["status"] == "deleted", deleted
    assert prepaid.settled_amount_fen == 0


@pytest.mark.parametrize(
    "field", ["rights_controlled", "acceptance_reference", "capitalization_basis"]
)
def test_stage_missing_facts_is_not_inferred(session, organization, sample_evidence, field):
    c = stage()
    c[field] = None
    result = ComponentService(session).record(
        request(organization, sample_evidence, "2022-09-21", "missing", [c])
    )
    assert result.status == "needs_information"
    assert f"components.stage.{field}" in result.missing_information
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 0


def test_configured_project_cost_account_survives_transfer_and_abandonment(
    session, organization, sample_evidence
):
    service = ComponentService(session)
    service.configure_account(
        ConfigureAccountRequest(
            org_id=organization.id,
            idempotency_key="cost-account",
            code="189902",
            name="另一项目成本",
            business_class="intangible_project_cost",
        )
    )
    first = record(
        session,
        request(
            organization,
            sample_evidence,
            "2022-09-21",
            "stage",
            [stage(account_selections={"intangible_project_cost": "189902"})],
        ),
    )
    source = {"component_id": component_id(first, "stage"), "amount_fen": 300000}
    canceled = record(
        session,
        request(
            organization,
            sample_evidence,
            "2022-10-01",
            "cancel-part",
            [
                facts(
                    "project_cost_expense",
                    "cancel",
                    "2022-10-01",
                    cost_sources=[source],
                    expense_class="general_expense",
                    reason="部分阶段成果废弃，不再形成资产",
                )
            ],
        ),
    )
    assert (
        session.scalar(
            select(VoucherLine).where(
                VoucherLine.voucher_id == canceled.voucher_id, VoucherLine.credit_fen > 0
            )
        ).account.code
        == "189902"
    )
    record(
        session,
        request(
            organization,
            sample_evidence,
            "2022-11-30",
            "asset",
            [asset("project_cost", [{**source, "amount_fen": 500000}], amount=500000)],
        ),
    )


def test_advance_balance_contention_and_future_sources_roll_back(
    session, organization, sample_evidence
):
    first = record(
        session,
        request(
            organization,
            sample_evidence,
            "2022-09-21",
            "advance",
            [advance()],
            [("advance", 800000)],
        ),
    )
    prepaid = item(session, first.event_id, "receivable")
    refunds = [
        facts(
            "supplier_advance_refund",
            key,
            "2022-09-22",
            counterparty=PARTY,
            payment_date="2022-09-22",
            refund_reference="partial-cancellation",
            advances=[{"open_item_id": prepaid.id, "amount_fen": 500000}],
        )
        for key in ("one", "two")
    ]
    rejected = ComponentService(session).record(
        request(
            organization,
            sample_evidence,
            "2022-09-22",
            "compete",
            refunds,
            [("one", 500000), ("two", 500000)],
            receipt=True,
        )
    )
    assert rejected.errors == ["SETTLEMENT_EXCEEDS_OPEN_BALANCE"]
    assert prepaid.settled_amount_fen == 0
    future = record(
        session, request(organization, sample_evidence, "2022-11-30", "future-asset", [asset()])
    )
    refused = ComponentService(session).record(
        request(
            organization,
            sample_evidence,
            "2022-09-21",
            "past-apply",
            [
                facts(
                    "supplier_advance_application",
                    "apply",
                    "2022-09-21",
                    counterparty=PARTY,
                    advances=[{"open_item_id": prepaid.id, "amount_fen": 800000}],
                    allocations=[
                        {
                            "open_item_id": item(session, future.event_id, "payable").id,
                            "amount_fen": 800000,
                        }
                    ],
                )
            ],
        )
    )
    assert refused.errors == ["SETTLEMENT_SOURCE_NOT_ACTIVE_OR_FUTURE"]
    assert prepaid.settled_amount_fen == 0


def test_development_conditions_and_mixed_cost_breakdown(session, organization, sample_evidence):
    c = stage()
    c.update(
        project_nature="internal_development",
        cost_element="directly_attributable_cost",
        development_conditions={
            "conditions_met_date": "2022-10-01",
            "technically_feasible": True,
            "intention_to_complete_and_use": True,
            "probable_economic_benefits": True,
            "adequate_resources": True,
            "reliably_measurable_cost": True,
        },
    )
    service = ComponentService(session)
    invalid = service.record(request(organization, sample_evidence, "2022-09-21", "too-early", [c]))
    assert invalid.errors == ["PROJECT_DEVELOPMENT_CAPITALIZATION_CONDITIONS_NOT_MET"]
    c["development_conditions"]["conditions_met_date"] = "2022-09-01"
    first = record(
        session, request(organization, sample_evidence, "2022-09-21", "development", [c])
    )
    accepted = asset(
        "project_cost",
        [{"component_id": component_id(first, "stage"), "amount_fen": 800000}],
        amount=800000,
    )
    accepted["facts"]["cost_components"].update(
        purchase_price_fen=0, directly_attributable_cost_fen=800000
    )
    record(session, request(organization, sample_evidence, "2022-11-30", "software", [accepted]))


def test_project_cost_balances_release_on_delete_and_reject_expense_sources(
    session, organization, sample_evidence
):
    from ai_accounting.models import BusinessEventComponent
    from ai_accounting.purchase_components import project_cost_balances

    first = record(
        session, request(organization, sample_evidence, "2022-09-21", "stage", [stage()])
    )
    parent = session.scalar(
        select(BusinessEventComponent).where(BusinessEventComponent.event_id == first.event_id)
    )
    source = {"component_id": str(parent.id), "amount_fen": 300000}
    second = record(
        session,
        request(
            organization,
            sample_evidence,
            "2022-10-01",
            "cancel",
            [
                facts(
                    "project_cost_expense",
                    "expense",
                    "2022-10-01",
                    cost_sources=[source],
                    expense_class="general_expense",
                    reason="阶段成果废弃",
                )
            ],
        ),
    )
    assert (
        project_cost_balances(session, organization.id, [parent])[str(parent.id)]["available_fen"]
        == 500000
    )
    wrong = ComponentService(session).record(
        request(
            organization,
            sample_evidence,
            "2022-11-30",
            "capitalize-expense",
            [
                asset(
                    "project_cost",
                    [{"component_id": component_id(second, "expense"), "amount_fen": 300000}],
                    amount=300000,
                )
            ],
        )
    )
    assert wrong.errors == ["SOURCE_COMPONENT_NOT_FOUND_OR_KIND_MISMATCH"]
    deleted = EventAmendmentService(session).amend(
        DeleteEventRequest(
            org_id=organization.id,
            event_id=second.event_id,
            idempotency_key="delete-expense",
            reason="撤回废弃认定",
            expected_facts_hash=second.data["facts_hash"],
        )
    )
    assert deleted["status"] == "deleted", deleted
    assert (
        project_cost_balances(session, organization.id, [parent])[str(parent.id)]["available_fen"]
        == 800000
    )


def test_prepaid_and_pending_cost_balance_sheet_and_cash_flow(
    session, organization, sample_evidence
):
    from datetime import date

    from ai_accounting.financial_statements import FinancialStatementService

    record(
        session,
        request(
            organization,
            sample_evidence,
            "2022-09-21",
            "advance",
            [advance()],
            [("advance", 800000)],
        ),
    )
    record(session, request(organization, sample_evidence, "2022-09-21", "stage", [stage()]))
    service = FinancialStatementService(session)
    rows = service._ledger_rows(organization.id, date(2022, 9, 30))
    missing = []
    balance = service._balance_sheet(rows, date(2022, 1, 1), date(2022, 9, 30), missing)
    assert balance[5]["ending_fen"] == 800000
    assert balance[28]["ending_fen"] == 800000
    cash = service._cash_flow_statement(
        rows, organization.id, date(2022, 7, 1), date(2022, 1, 1), date(2022, 9, 30), missing
    )
    assert cash[12]["current_fen"] == 800000
    assert not missing


def test_supplier_advance_and_fee_match_one_imported_bank_row(committable_session):
    from conftest import import_test_bank_transaction, prepare_authenticated_bank_account

    from ai_accounting.coa import seed_organization
    from ai_accounting.models import BankTransactionMatch, Evidence

    session = committable_session
    organization = seed_organization(
        session,
        name="预付银行测试",
        taxpayer_identification_number="91330106MA1234567T",
        accounting_period_control_enabled=False,
    )
    prepare_authenticated_bank_account(session, organization)
    sample_evidence = session.scalar(select(Evidence).where(Evidence.org_id == organization.id))
    bank = import_test_bank_transaction(
        session, organization, amount_fen=-800300, key="advance-and-fee"
    )
    req = request(
        organization,
        sample_evidence,
        "2026-03-05",
        "advance-with-fee",
        [
            advance(day="2026-03-05"),
            facts(
                "expense",
                "fee",
                "2026-03-05",
                amount_fen=300,
                expense_class="finance_expense",
                expense_nature="bank_service_fee",
                payment_basis="immediate",
            ),
        ],
        [("advance", 800000), ("fee", 300)],
    ).model_dump(mode="json")
    req["funds"][0].update(account_code="1002", bank_transaction_references=[{"id": str(bank.id)}])
    typed = RecordEventRequest.model_validate(req)
    first = record(session, typed)
    assert record(session, typed).event_id == first.event_id
    assert session.scalar(select(func.count()).select_from(BankTransactionMatch)) == 1


def test_purchase_components_are_discoverable_through_mcp():
    from ai_accounting.mcp_server import finance_get_event_schema

    for kind in (
        "supplier_advance",
        "supplier_advance_application",
        "supplier_advance_refund",
        "project_cost",
        "project_cost_expense",
    ):
        result = finance_get_event_schema(component_type=kind)
        assert result["status"] == "ok"
        assert result["selected_component_type"] == kind
        assert result["component_schema"]["$ref"]


def test_get_event_exposes_remaining_project_cost(
    session, organization, sample_evidence, monkeypatch
):
    from ai_accounting import mcp_server

    posted = record(
        session, request(organization, sample_evidence, "2022-09-21", "stage", [stage()])
    )
    monkeypatch.setattr(mcp_server, "SessionLocal", lambda: session)
    result = mcp_server.finance_get_event(str(organization.id), str(posted.event_id))
    assert result["status"] == "ok"
    assert result["components"][0]["project_cost_balance"] == {
        "project_reference": PROJECT,
        "recognized_fen": 800000,
        "consumed_fen": 0,
        "available_fen": 800000,
    }
