from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from ai_accounting.component_schemas import ConfigureAccountRequest, RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventComponent,
    OpenItem,
    Voucher,
    VoucherLine,
)


@pytest.fixture
def sample_evidence(session, organization):
    from ai_accounting.models import Evidence

    item = Evidence(
        org_id=organization.id,
        original_name="component-evidence.txt",
        storage_path="component-evidence.txt",
        sha256="a" * 64,
        size_bytes=1,
        media_type="text/plain",
        source="test",
    )
    session.add(item)
    session.flush()
    return item


def expense(key, amount, category="general_expense", **changes):
    return {
        "key": key,
        "kind": "expense",
        "business_date": "2026-03-05",
        "payment_date": "2026-03-05",
        "amount_fen": amount,
        "expense_class": category,
        "payment_basis": "immediate",
        **changes,
    }


def request(organization, evidence, components, *, key="components-1", amounts=None):
    amounts = amounts or [(c["key"], c["amount_fen"]) for c in components]
    return RecordEventRequest(
        org_id=organization.id,
        idempotency_key=key,
        posting_date=date(2026, 3, 5),
        evidence_references=[evidence.id],
        components=components,
        funds=[
            {
                "key": "cash",
                "account_code": "1001",
                "direction": "payment",
                "payment_date": "2026-03-05",
                "amount_fen": sum(a for _, a in amounts),
                "allocations": [{"component_key": k, "amount_fen": a} for k, a in amounts],
            }
        ],
    )


def test_new_account_and_two_expense_classes_share_one_voucher(
    session, organization, sample_evidence
):
    service = ComponentService(session)
    service.configure_account(
        ConfigureAccountRequest(
            org_id=organization.id,
            idempotency_key="travel-class",
            code="560209",
            name="差旅费",
            business_class="general_expense",
        )
    )
    payload = request(
        organization,
        sample_evidence,
        [expense("travel", 101, account_code="560209"), expense("selling", 202, "sales_expense")],
    )
    result = service.record(payload)
    assert result.status == "posted", result
    assert session.scalar(select(func.count()).select_from(Voucher)) == 1
    assert session.scalar(select(func.count()).select_from(BusinessEventComponent)) == 3
    lines = session.scalars(
        select(VoucherLine).where(VoucherLine.voucher_id == result.voucher_id)
    ).all()
    assert sum(line.debit_fen for line in lines) == sum(line.credit_fen for line in lines) == 303
    assert {line.account.code for line in lines} == {"560209", "5601", "1001"}
    assert service.record(payload).event_id == result.event_id
    assert service.record(payload.model_copy(update={"description": "management summary"})).data[
        "idempotent_replay"
    ]
    changed = payload.model_copy(
        update={
            "components": [
                payload.components[0].model_copy(update={"amount_fen": 102}),
                payload.components[1],
            ]
        }
    )
    assert service.record(changed).errors == ["IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"]


def test_missing_component_fact_rolls_back_entire_event(session, organization, sample_evidence):
    payload = request(
        organization,
        sample_evidence,
        [expense("valid", 100), expense("incomplete", 200, expense_class=None)],
    )
    result = ComponentService(session).record(payload)
    assert result.status == "needs_information", result
    assert result.missing_information == ["components.incomplete.expense_class"]
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 0
    assert session.scalar(select(func.count()).select_from(Voucher)) == 0


def test_create_and_settle_obligation_in_same_event(session, organization, sample_evidence):
    party = {"kind": "supplier", "name": "Supplier"}
    components = [
        expense("purchase", 100, payment_basis="supplier_credit", metadata={"counterparty": party}),
        {
            "key": "settle",
            "kind": "payable_settlement",
            "business_date": "2026-03-05",
            "payment_date": "2026-03-05",
            "allocations": [{"source_component_key": "purchase", "amount_fen": 100}],
            "metadata": {"counterparty": party},
        },
    ]
    result = ComponentService(session).record(
        request(organization, sample_evidence, components, amounts=[("settle", 100)])
    )
    assert result.status == "posted", result
    item = session.scalar(select(OpenItem))
    assert item.settled_amount_fen == item.original_amount_fen == 100


def test_component_graph_and_money_types_are_strict(organization, sample_evidence):
    with pytest.raises(ValidationError):
        request(organization, sample_evidence, [expense("float", 12.0)])
    with pytest.raises(ValidationError, match="cyclic"):
        request(
            organization,
            sample_evidence,
            [expense("a", 1, depends_on=["b"]), expense("b", 1, depends_on=["a"])],
        )
    with pytest.raises(ValidationError):
        request(organization, sample_evidence, [expense("free", 1, debit_fen=1)])


def test_configured_payable_detail_is_inherited_by_local_settlement(
    session,
    organization,
    sample_evidence,
):
    service = ComponentService(session)
    configuration = ConfigureAccountRequest(
        org_id=organization.id,
        idempotency_key="supplier-detail",
        code="220299",
        name="服务供应商应付",
        business_class="accounts_payable",
    )
    configured = service.configure_account(configuration)
    assert service.configure_account(configuration)["account_id"] == configured["account_id"]
    with pytest.raises(ValueError, match="IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"):
        service.configure_account(configuration.model_copy(update={"code": "220298"}))
    party = {"kind": "supplier", "name": "Supplier detail"}
    components = [
        expense(
            "purchase",
            100,
            payment_basis="supplier_credit",
            metadata={"counterparty": party},
            account_selections={"accounts_payable": "220299"},
        ),
        {
            "key": "payment",
            "kind": "payable_settlement",
            "business_date": "2026-03-05",
            "payment_date": "2026-03-05",
            "allocations": [{"source_component_key": "purchase", "amount_fen": 100}],
            "metadata": {"counterparty": party},
        },
    ]
    result = service.record(
        request(
            organization,
            sample_evidence,
            components,
            amounts=[("payment", 100)],
        )
    )
    assert result.status == "posted", result
    item = session.scalar(select(OpenItem))
    assert str(item.account_id) == configured["account_id"]
    lines = session.scalars(
        select(VoucherLine).where(VoucherLine.account_id == item.account_id)
    ).all()
    assert sum(line.debit_fen for line in lines) == sum(line.credit_fen for line in lines) == 100


def test_missing_amount_has_no_formal_write(session, organization, sample_evidence):
    payload = request(organization, sample_evidence, [expense("unknown", 100)])
    incomplete = payload.model_dump(mode="json")
    incomplete["components"][0].pop("amount_fen")
    result = ComponentService(session).record(RecordEventRequest.model_validate(incomplete))
    assert result.status == "needs_information", result
    assert result.missing_information == ["components.unknown.amount_fen"]
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 0


def test_advance_fulfillment_keeps_configured_source_balance_account(
    session, organization, sample_evidence
):
    service = ComponentService(session)
    service.configure_account(
        ConfigureAccountRequest(
            org_id=organization.id,
            idempotency_key="advance-detail",
            code="220309",
            name="服务合同预收款",
            business_class="contract_liability",
        )
    )
    day = "2026-03-05"
    party = {"kind": "customer", "name": "合同客户"}
    common = {"business_date": day, "metadata": {"counterparty": party}}
    result = service.record(
        RecordEventRequest(
            org_id=organization.id,
            idempotency_key="advance-and-fulfill",
            posting_date=day,
            evidence_references=[sample_evidence.id],
            components=[
                {
                    "key": "advance",
                    "kind": "customer_advance",
                    "amount_fen": 100,
                    "tax_facts": {"tax_due_on_event": False},
                    "account_selections": {"contract_liability": "220309"},
                    **common,
                },
                {
                    "key": "fulfill",
                    "kind": "service_fulfillment",
                    "amount_fen": 100,
                    "source": {"component_key": "advance"},
                    "fulfillment_date": day,
                    "tax_facts": {
                        "taxable": False,
                        "invoice_type": "none",
                        "waive_exemption": False,
                        "tax_due_on_event": False,
                    },
                    **common,
                },
            ],
            funds=[
                {
                    "key": "receipt",
                    "account_code": "1001",
                    "direction": "receipt",
                    "payment_date": day,
                    "amount_fen": 100,
                    "allocations": [{"component_key": "advance", "amount_fen": 100}],
                }
            ],
        )
    )
    assert result.status == "posted", result
    lines = list(
        session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == result.voucher_id))
    )
    source_lines = [line for line in lines if line.account.code == "220309"]
    assert len(source_lines) == 2
    assert sum(line.debit_fen - line.credit_fen for line in source_lines) == 0


def test_taxed_advance_refund_reduces_its_component_tax_source(
    session, organization, sample_evidence
):
    from ai_accounting.tax import calculate_tax_period

    day = "2026-03-05"
    facts = {
        "business_date": day,
        "payment_date": day,
        "amount_fen": 10100,
        "metadata": {"counterparty": {"kind": "customer", "name": "退款客户"}},
        "tax_obligation_date": day,
        "tax_facts": {
            "taxable": True,
            "rate_percent": "1",
            "invoice_type": "ordinary",
            "waive_exemption": False,
            "tax_due_on_event": True,
        },
    }
    payload = RecordEventRequest(
        org_id=organization.id,
        idempotency_key="taxed-advance-refund",
        posting_date=day,
        evidence_references=[sample_evidence.id],
        components=[
            {"key": "advance", "kind": "customer_advance", **facts},
            {
                "key": "refund",
                "kind": "customer_refund",
                "refund_kind": "advance",
                "source": {"component_key": "advance"},
                **facts,
            },
        ],
        funds=[
            {
                "key": key,
                "account_code": "1001",
                "direction": direction,
                "payment_date": day,
                "amount_fen": 10100,
                "allocations": [{"component_key": key, "amount_fen": 10100}],
            }
            for key, direction in [("advance", "receipt"), ("refund", "payment")]
        ],
    )
    result = ComponentService(session).record(payload)
    assert result.status == "posted", result
    period = calculate_tax_period(
        session, organization, date(2026, 1, 1), date(2026, 3, 31), date(2026, 3, 31)
    )
    assert period.gross_sales_fen == period.net_sales_fen == period.vat_accrued_fen == 0


@pytest.mark.parametrize("usage_kind", ["service_fulfillment", "customer_refund"])
def test_local_source_usage_survives_later_reference_and_rounding(
    session, organization, sample_evidence, usage_kind
):
    from ai_accounting.models import Account, BusinessEventComponent

    service = ComponentService(session)
    day = "2026-03-05"
    common = {
        "business_date": day,
        "payment_date": day,
        "metadata": {"counterparty": {"kind": "customer", "name": "分次结算客户"}},
        "tax_obligation_date": day,
        "tax_facts": {
            "taxable": True,
            "rate_percent": "3",
            "invoice_type": "ordinary",
            "waive_exemption": False,
            "tax_due_on_event": True,
        },
    }

    def usage(key, reference, amount):
        return {
            "key": key,
            "kind": usage_kind,
            "amount_fen": amount,
            "source": reference,
            **common,
            **(
                {"fulfillment_date": day}
                if usage_kind == "service_fulfillment"
                else {"refund_kind": "advance"}
            ),
        }

    def funds(key, direction, amount):
        return {
            "key": key,
            "account_code": "1001",
            "direction": direction,
            "payment_date": day,
            "amount_fen": amount,
            "allocations": [{"component_key": key, "amount_fen": amount}],
        }

    def post(key, components, funding):
        return service.record(
            RecordEventRequest(
                org_id=organization.id,
                idempotency_key=key,
                posting_date=day,
                evidence_references=[sample_evidence.id],
                components=components,
                funds=funding,
            )
        )

    first = post(
        "local-usage",
        [
            {"key": "advance", "kind": "customer_advance", "amount_fen": 100, **common},
            usage("part-one", {"component_key": "advance"}, 50),
        ],
        [funds("advance", "receipt", 100)]
        + ([funds("part-one", "payment", 50)] if usage_kind == "customer_refund" else []),
    )
    assert first.status == "posted", first
    source = session.scalar(
        select(BusinessEventComponent).where(
            BusinessEventComponent.event_id == first.event_id,
            BusinessEventComponent.key == "advance",
        )
    )
    reference = {"component_id": str(source.id)}
    excessive = post(
        "too-much",
        [usage("over", reference, 51)],
        [funds("over", "payment", 51)] if usage_kind == "customer_refund" else [],
    )
    assert excessive.status == "rejected", excessive
    assert "COMPONENT_SOURCE_AMOUNT_EXCEEDED" in str(excessive)
    second = post(
        "remaining",
        [usage("part-two", reference, 50)],
        [funds("part-two", "payment", 50)] if usage_kind == "customer_refund" else [],
    )
    assert second.status == "posted", second
    liability = session.scalar(select(Account).where(Account.system_role == "contract_liability"))
    lines = session.scalars(select(VoucherLine).where(VoucherLine.account_id == liability.id))
    assert sum(line.debit_fen - line.credit_fen for line in lines) == 0
    if usage_kind == "customer_refund":
        vat = session.scalar(select(Account).where(Account.system_role == "vat_payable"))
        lines = session.scalars(select(VoucherLine).where(VoucherLine.account_id == vat.id))
        assert sum(line.debit_fen - line.credit_fen for line in lines) == 0


@pytest.mark.parametrize("include_receipt", [False, True])
def test_credit_sale_cash_refund_uses_whole_plan_received_balance(
    session, organization, sample_evidence, include_receipt
):
    day = "2026-03-05"
    party = {"kind": "customer", "name": "Credit refund customer"}
    common = {
        "business_date": day,
        "payment_date": day,
        "metadata": {"counterparty": party},
        "tax_obligation_date": day,
        "tax_facts": {
            "taxable": True,
            "rate_percent": "3",
            "invoice_type": "ordinary",
            "waive_exemption": False,
            "tax_due_on_event": True,
        },
    }
    components = [
        {
            "key": "refund",
            "kind": "customer_refund",
            "amount_fen": 10300,
            "refund_kind": "sale_return",
            "source": {"component_key": "sale"},
            **common,
        },
        {
            "key": "sale",
            "kind": "service_sale",
            "amount_fen": 10300,
            "recognition_basis": "credit",
            "fulfillment_date": day,
            **common,
        },
    ]
    if include_receipt:
        components.append(
            {
                "key": "receipt",
                "kind": "receivable_settlement",
                "business_date": day,
                "payment_date": day,
                "allocations": [{"source_component_key": "sale", "amount_fen": 10300}],
                "metadata": {"counterparty": party},
            }
        )
    funding = [
        {
            "key": key,
            "account_code": "1001",
            "direction": direction,
            "payment_date": day,
            "amount_fen": 10300,
            "allocations": [{"component_key": key, "amount_fen": 10300}],
        }
        for key, direction in ([("receipt", "receipt")] if include_receipt else [])
        + [("refund", "payment")]
    ]
    result = ComponentService(session).record(
        RecordEventRequest(
            org_id=organization.id,
            idempotency_key="credit-cash-refund",
            posting_date=day,
            evidence_references=[sample_evidence.id],
            components=components,
            funds=funding,
        )
    )
    if include_receipt:
        assert result.status == "posted", result
        assert session.scalar(select(OpenItem)).status == "settled"
        balances = {}
        for line in session.scalars(select(VoucherLine)):
            balances[line.account_id] = (
                balances.get(line.account_id, 0) + line.debit_fen - line.credit_fen
            )
        assert set(balances.values()) == {0}
    else:
        assert result.status == "rejected", result
        assert result.errors == ["CUSTOMER_REFUND_EXCEEDS_RECEIVED_AMOUNT"]
        assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 0
