from __future__ import annotations

from calendar import monthrange
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ai_accounting.intangible_asset_schemas import (
    AcquireIntangibleAssetRequest,
    ConfirmIntangibleAssetAmortizationRequest,
    PreviewIntangibleAssetAmortizationRequest,
    RetireIntangibleAssetRequest,
)
from ai_accounting.intangible_asset_service import IntangibleAssetService
from ai_accounting.models import (
    BankTransaction,
    BusinessEvent,
    Counterparty,
    Evidence,
    IntangibleAsset,
    IntangibleAssetAmortization,
    IntangibleAssetRetirement,
    OpenItem,
    Organization,
    VoucherLine,
)
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService


def _evidence(session: Session, organization: Organization, seed: str) -> Evidence:
    row = Evidence(
        org_id=organization.id,
        sha256=(seed * 64)[:64],
        original_name=f"{seed}.pdf",
        media_type="application/pdf",
        source="test",
        size_bytes=1,
        storage_path=f"test/{seed}",
    )
    session.add(row)
    session.flush()
    return row


def _request(
    organization: Organization,
    evidence: Evidence,
    *,
    key: str = "intangible-acquire-1",
    asset_code: str = "IA-001",
) -> AcquireIntangibleAssetRequest:
    return AcquireIntangibleAssetRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "asset_code": asset_code,
            "asset_name": "外购软件许可",
            "category": "software",
            "rights_description": "三年可续的软件使用许可",
            "supplier": {"kind": "supplier", "name": "软件供应商"},
            "acquisition_date": "2026-01-02",
            "available_for_use_date": "2026-01-02",
            "posting_date": "2026-01-02",
            "cost_components": {
                "purchase_price_fen": 11_000,
                "noncreditable_tax_fen": 500,
                "directly_attributable_cost_fen": 500,
            },
            "settlement_method": "payable",
            "due_date": "2026-02-02",
            "benefit_area": "management",
            "life_basis": "legal_or_contractual",
            "useful_life_months": 12,
            "life_basis_explanation": "合同约定十二个月许可期",
            "is_available_for_use": True,
            "claims_creditable_input_vat": False,
            "evidence_references": [evidence.id],
        }
    )


def _assert_balanced(session: Session, voucher_id: object) -> None:
    lines = session.scalars(
        select(VoucherLine).where(VoucherLine.voucher_id == voucher_id)
    ).all()
    assert sum(line.debit_fen for line in lines) == sum(line.credit_fen for line in lines)
    assert sum(line.debit_fen for line in lines) > 0


def test_intangible_asset_write_preserves_period_control_error(
    session: Session, organization: Organization
) -> None:
    organization.accounting_period_control_enabled = True
    organization.accounting_period_control_start_date = None
    evidence = _evidence(session, organization, "period-ia")

    result = IntangibleAssetService(session).acquire_intangible_asset(
        _request(
            organization,
            evidence,
            key="intangible-period-not-generated",
            asset_code="IA-PERIOD",
        )
    )

    assert result.status == "rejected"
    assert result.errors == ["ACCOUNTING_PERIOD_NOT_GENERATED"]
    assert result.event_id is None
    assert result.voucher_id is None


def test_acquisition_is_normalized_balanced_and_payload_idempotent(
    session: Session, organization: Organization
) -> None:
    evidence = _evidence(session, organization, "ia")
    service = IntangibleAssetService(session)
    request = _request(organization, evidence)

    posted = service.acquire_intangible_asset(request)

    assert posted.status == "posted", posted.errors
    assert posted.data["cost_fen"] == 12_000
    assert posted.data["residual_value_fen"] == 0
    _assert_balanced(session, posted.voucher_id)
    asset = session.get(IntangibleAsset, posted.asset_id)
    assert asset.acquisition_event_id == posted.event_id
    assert asset.available_for_use_date == date(2026, 1, 2)
    assert asset.useful_life_months == 12
    assert asset.is_available_for_use is True
    assert asset.claims_creditable_input_vat is False
    event = session.get(BusinessEvent, posted.event_id)
    assert event.facts["accounting_rule_version"] == asset.accounting_rule_version
    assert event.facts["accounting_rule_source_url"] == asset.accounting_rule_source_url
    payable = session.scalar(select(OpenItem).where(OpenItem.source_event_id == posted.event_id))
    assert payable.original_amount_fen == 12_000
    assert payable.item_type == "payable"
    assert any(item["stage"] == "entries_created" for item in posted.trace)

    replay = service.acquire_intangible_asset(request)
    assert replay.event_id == posted.event_id
    assert replay.data["idempotent_replay"] is True
    session.flush()
    session.expire_all()
    reloaded = service.acquire_intangible_asset(request)
    assert reloaded.asset_id == posted.asset_id
    assert reloaded.data["cost_fen"] == 12_000

    changed = service.acquire_intangible_asset(
        request.model_copy(update={"asset_name": "换载荷名称"})
    )
    assert changed.errors == ["INTANGIBLE_ASSET_IDEMPOTENCY_PAYLOAD_MISMATCH"]


def test_missing_readiness_and_unsupported_workflows_are_stable(
    session: Session, organization: Organization
) -> None:
    evidence = _evidence(session, organization, "ib")
    service = IntangibleAssetService(session)
    missing_request = AcquireIntangibleAssetRequest(
        org_id=organization.id,
        idempotency_key="intangible-missing",
    )
    missing = service.acquire_intangible_asset(missing_request)
    assert missing.status == "needs_information"
    assert {item.code for item in missing.missing_information} >= {
        "INTANGIBLE_ASSET_IDENTITY_REQUIRED",
        "INTANGIBLE_ASSET_COST_COMPONENTS_REQUIRED",
        "INTANGIBLE_ASSET_EVIDENCE_REQUIRED",
    }
    missing_replay = service.acquire_intangible_asset(missing_request)
    assert missing_replay.event_id == missing.event_id
    assert missing_replay.data["idempotent_replay"] is True
    missing_mismatch = service.acquire_intangible_asset(
        missing_request.model_copy(update={"asset_name": "不同载荷"})
    )
    assert missing_mismatch.errors == [
        "INTANGIBLE_ASSET_IDEMPOTENCY_PAYLOAD_MISMATCH"
    ]

    not_ready = service.acquire_intangible_asset(
        _request(
            organization,
            evidence,
            key="intangible-not-ready",
            asset_code="IA-NOT-READY",
        ).model_copy(update={"is_available_for_use": False})
    )
    assert not_ready.errors == ["INTANGIBLE_ASSET_NOT_READY_WORKFLOW_NOT_ENABLED"]
    not_ready_replay = service.acquire_intangible_asset(
        _request(
            organization,
            evidence,
            key="intangible-not-ready",
            asset_code="IA-NOT-READY",
        ).model_copy(update={"is_available_for_use": False})
    )
    assert not_ready_replay.event_id == not_ready.event_id
    not_ready_mismatch = service.acquire_intangible_asset(
        _request(
            organization,
            evidence,
            key="intangible-not-ready",
            asset_code="IA-NOT-READY-CHANGED",
        ).model_copy(update={"is_available_for_use": False})
    )
    assert not_ready_mismatch.errors == [
        "INTANGIBLE_ASSET_IDEMPOTENCY_PAYLOAD_MISMATCH"
    ]
    creditable = service.acquire_intangible_asset(
        _request(
            organization,
            evidence,
            key="intangible-creditable",
            asset_code="IA-CREDITABLE",
        ).model_copy(update={"claims_creditable_input_vat": True})
    )
    assert creditable.errors == ["INTANGIBLE_ASSET_CREDITABLE_INPUT_VAT_NOT_ENABLED"]


def test_bank_acquisition_requires_exact_outflow_and_matches_it(
    session: Session, organization: Organization
) -> None:
    evidence = _evidence(session, organization, "ic")
    bank = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint=("bank-ia" * 64)[:64],
        booking_date=date(2026, 1, 2),
        amount_fen=-12_000,
        currency="CNY",
        memo="software purchase",
        source_sha256=("source-ia" * 64)[:64],
    )
    session.add(bank)
    session.flush()
    request_payload = _request(
        organization,
        evidence,
        key="intangible-bank",
        asset_code="IA-BANK",
    ).model_dump(mode="python")
    request_payload.update(
        {
            "settlement_method": "bank",
            "payment_date": date(2026, 1, 2),
            "due_date": None,
            "bank_transaction_references": [{"id": bank.id}],
        }
    )
    request = AcquireIntangibleAssetRequest.model_validate(request_payload)
    posted = IntangibleAssetService(session).acquire_intangible_asset(request)
    assert posted.status == "posted", posted.errors
    assert bank.matched_event_id == posted.event_id
    assert session.scalar(
        select(OpenItem).where(OpenItem.source_event_id == posted.event_id)
    ) is None
    _assert_balanced(session, posted.voucher_id)

    foreign_currency = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint=("bank-ia-usd" * 64)[:64],
        booking_date=date(2026, 1, 2),
        amount_fen=-12_000,
        currency="CNY",
        memo="unsupported currency",
        source_sha256=("source-ia-usd" * 64)[:64],
    )
    session.add(foreign_currency)
    session.flush()
    # The shared bank model already rejects non-CNY at storage time. Simulate
    # legacy/corrupt loaded state without dirtying the row to exercise the
    # service's independent defensive boundary.
    set_committed_value(foreign_currency, "currency", "USD")
    foreign_payload = _request(
        organization,
        evidence,
        key="intangible-bank-usd",
        asset_code="IA-BANK-USD",
    ).model_dump(mode="python")
    foreign_payload.update(
        {
            "settlement_method": "bank",
            "payment_date": date(2026, 1, 2),
            "due_date": None,
            "bank_transaction_references": [{"id": foreign_currency.id}],
        }
    )
    result = IntangibleAssetService(session).acquire_intangible_asset(
        AcquireIntangibleAssetRequest.model_validate(foreign_payload)
    )
    assert result.errors == ["INTANGIBLE_ASSET_BANK_TRANSACTION_CURRENCY_MISMATCH"]
    assert foreign_currency.matched_event_id is None


def test_supplier_identity_cost_sum_and_calendar_bounds_are_strict(
    session: Session, organization: Organization
) -> None:
    evidence = _evidence(session, organization, "ih")
    service = IntangibleAssetService(session)
    initial_payload = _request(
        organization,
        evidence,
        key="intangible-supplier-initial",
        asset_code="IA-SUPPLIER-1",
    ).model_dump(mode="python")
    initial_payload["supplier"] = {
        "kind": " supplier ",
        "name": " 严格供应商 ",
        "external_ref": " SUP-001 ",
    }
    initial = service.acquire_intangible_asset(
        AcquireIntangibleAssetRequest.model_validate(initial_payload)
    )
    assert initial.status == "posted", initial.errors
    asset = session.get(IntangibleAsset, initial.asset_id)
    supplier = session.get(Counterparty, asset.supplier_id)
    assert supplier.name == "严格供应商"
    assert supplier.external_ref == "SUP-001"

    id_mismatch_payload = _request(
        organization,
        evidence,
        key="intangible-supplier-id-mismatch",
        asset_code="IA-SUPPLIER-2",
    ).model_dump(mode="python")
    id_mismatch_payload["supplier"] = {
        "id": supplier.id,
        "kind": "supplier",
        "name": "错误名称",
        "external_ref": "SUP-001",
    }
    id_mismatch = service.acquire_intangible_asset(
        AcquireIntangibleAssetRequest.model_validate(id_mismatch_payload)
    )
    assert id_mismatch.errors == ["INTANGIBLE_ASSET_COUNTERPARTY_IDENTITY_MISMATCH"]

    name_mismatch_payload = _request(
        organization,
        evidence,
        key="intangible-supplier-name-mismatch",
        asset_code="IA-SUPPLIER-3",
    ).model_dump(mode="python")
    name_mismatch_payload["supplier"] = {
        "kind": "supplier",
        "name": "严格供应商",
        "external_ref": "SUP-CONFLICT",
    }
    name_mismatch = service.acquire_intangible_asset(
        AcquireIntangibleAssetRequest.model_validate(name_mismatch_payload)
    )
    assert name_mismatch.errors == [
        "INTANGIBLE_ASSET_COUNTERPARTY_IDENTITY_MISMATCH"
    ]

    external_mismatch_payload = _request(
        organization,
        evidence,
        key="intangible-supplier-external-mismatch",
        asset_code="IA-SUPPLIER-4",
    ).model_dump(mode="python")
    external_mismatch_payload["supplier"] = {
        "kind": "supplier",
        "name": "另一个名称",
        "external_ref": "SUP-001",
    }
    external_mismatch = service.acquire_intangible_asset(
        AcquireIntangibleAssetRequest.model_validate(external_mismatch_payload)
    )
    assert external_mismatch.errors == [
        "INTANGIBLE_ASSET_COUNTERPARTY_IDENTITY_MISMATCH"
    ]

    range_request = _request(
        organization,
        evidence,
        key="intangible-cost-range",
        asset_code="IA-RANGE",
    )
    range_request = range_request.model_copy(
        update={
            "cost_components": range_request.cost_components.model_copy(
                update={
                    "purchase_price_fen": 2**63 - 1,
                    "noncreditable_tax_fen": 1,
                    "directly_attributable_cost_fen": 0,
                }
            )
        }
    )
    range_result = service.acquire_intangible_asset(range_request)
    assert range_result.errors == ["INTANGIBLE_ASSET_COST_OUT_OF_RANGE"]

    calendar_payload = _request(
        organization,
        evidence,
        key="intangible-calendar-range",
        asset_code="IA-CALENDAR",
    ).model_dump(mode="python")
    calendar_payload.update(
        {
            "acquisition_date": date(9999, 12, 1),
            "available_for_use_date": date(9999, 12, 1),
            "posting_date": date(9999, 12, 1),
            "due_date": date(9999, 12, 31),
            "useful_life_months": 2,
        }
    )
    calendar_result = service.acquire_intangible_asset(
        AcquireIntangibleAssetRequest.model_validate(calendar_payload)
    )
    assert calendar_result.errors == [
        "INTANGIBLE_ASSET_AMORTIZATION_DATE_OUT_OF_RANGE"
    ]

    boundary_payload = dict(calendar_payload)
    boundary_payload.update(
        {
            "idempotency_key": "intangible-calendar-boundary",
            "asset_code": "IA-CALENDAR-BOUNDARY",
            "useful_life_months": 1,
        }
    )
    boundary = service.acquire_intangible_asset(
        AcquireIntangibleAssetRequest.model_validate(boundary_payload)
    )
    assert boundary.status == "posted", boundary.errors
    boundary_preview_request = PreviewIntangibleAssetAmortizationRequest(
        org_id=organization.id,
        asset_id=boundary.asset_id,
        amortization_period="9999-12",
        posting_date=date(9999, 12, 31),
    )
    boundary_preview = service.preview_intangible_asset_amortization(
        boundary_preview_request
    )
    assert boundary_preview.status == "calculated", boundary_preview.errors
    boundary_amortization = service.confirm_intangible_asset_amortization(
        ConfirmIntangibleAssetAmortizationRequest(
            **boundary_preview_request.model_dump(),
            idempotency_key="intangible-calendar-boundary-amortization",
            calculation_hash=boundary_preview.calculation_hash,
            confirmed_by="tester",
        )
    )
    assert boundary_amortization.status == "posted", boundary_amortization.errors
    boundary_retirement = service.retire_intangible_asset(
        RetireIntangibleAssetRequest(
            org_id=organization.id,
            asset_id=boundary.asset_id,
            idempotency_key="intangible-calendar-boundary-retirement",
            retirement_date=date(9999, 12, 31),
            posting_date=date(9999, 12, 31),
            gross_proceeds_fen=0,
            compensation_fen=0,
            taxes_and_fees_fen=0,
            residual_proceeds_fen=0,
            evidence_references=[evidence.id],
        )
    )
    assert boundary_retirement.status == "posted", boundary_retirement.errors


def test_amortization_hash_sequence_retirement_and_reverse_order(
    session: Session, organization: Organization
) -> None:
    evidence = _evidence(session, organization, "id")
    retirement_evidence = _evidence(session, organization, "ie")
    service = IntangibleAssetService(session)
    acquired = service.acquire_intangible_asset(
        _request(
            organization,
            evidence,
            key="intangible-lifecycle-acquire",
            asset_code="IA-LIFECYCLE",
        )
    )
    preview_request = PreviewIntangibleAssetAmortizationRequest(
        org_id=organization.id,
        asset_id=acquired.asset_id,
        amortization_period="2026-01",
        posting_date=date(2026, 1, 31),
    )
    invalid_year = service.preview_intangible_asset_amortization(
        preview_request.model_copy(update={"amortization_period": "0000-01"})
    )
    assert invalid_year.errors == ["INTANGIBLE_ASSET_AMORTIZATION_PERIOD_INVALID"]
    skipped = service.preview_intangible_asset_amortization(
        preview_request.model_copy(
            update={
                "amortization_period": "2026-02",
                "posting_date": date(2026, 2, 28),
            }
        )
    )
    assert skipped.errors == ["INTANGIBLE_ASSET_AMORTIZATION_OUT_OF_SEQUENCE"]
    preview = service.preview_intangible_asset_amortization(preview_request)
    assert preview.status == "calculated"
    assert preview.data["amortization_fen"] == 1_000
    assert preview.data["sequence_no"] == 1

    stale_request = ConfirmIntangibleAssetAmortizationRequest(
        **preview_request.model_dump(),
        idempotency_key="intangible-amortization-stale",
        calculation_hash="0" * 64,
        confirmed_by="tester",
    )
    stale = service.confirm_intangible_asset_amortization(stale_request)
    assert stale.errors == ["INTANGIBLE_ASSET_CALCULATION_STALE"]

    confirm_request = stale_request.model_copy(
        update={
            "idempotency_key": "intangible-amortization-confirm",
            "calculation_hash": preview.calculation_hash,
        }
    )
    confirmed = service.confirm_intangible_asset_amortization(confirm_request)
    assert confirmed.status == "posted", confirmed.errors
    assert confirmed.data["closing_accumulated_amortization_fen"] == 1_000
    _assert_balanced(session, confirmed.voucher_id)
    row = session.scalar(
        select(IntangibleAssetAmortization).where(
            IntangibleAssetAmortization.event_id == confirmed.event_id
        )
    )
    assert row.period_start == date(2026, 1, 1)
    assert row.calculation_hash == preview.calculation_hash
    assert any(item["stage"] == "entries_created" for item in confirmed.trace)
    replay = service.confirm_intangible_asset_amortization(confirm_request)
    assert replay.event_id == confirmed.event_id
    mismatch = service.confirm_intangible_asset_amortization(
        confirm_request.model_copy(update={"confirmation_note": "换载荷"})
    )
    assert mismatch.errors == ["INTANGIBLE_ASSET_IDEMPOTENCY_PAYLOAD_MISMATCH"]

    nonzero_retirement = service.retire_intangible_asset(
        RetireIntangibleAssetRequest(
            org_id=organization.id,
            asset_id=acquired.asset_id,
            idempotency_key="intangible-retire-nonzero",
            retirement_date=date(2026, 1, 31),
            posting_date=date(2026, 1, 31),
            gross_proceeds_fen=1,
            compensation_fen=0,
            taxes_and_fees_fen=0,
            residual_proceeds_fen=0,
            evidence_references=[retirement_evidence.id],
        )
    )
    assert nonzero_retirement.errors == [
        "INTANGIBLE_ASSET_RETIREMENT_ZERO_FACTS_REQUIRED"
    ]
    midmonth_retirement = service.retire_intangible_asset(
        RetireIntangibleAssetRequest(
            org_id=organization.id,
            asset_id=acquired.asset_id,
            idempotency_key="intangible-retire-midmonth",
            retirement_date=date(2026, 1, 30),
            posting_date=date(2026, 1, 30),
            gross_proceeds_fen=0,
            compensation_fen=0,
            taxes_and_fees_fen=0,
            residual_proceeds_fen=0,
            evidence_references=[retirement_evidence.id],
        )
    )
    assert midmonth_retirement.errors == ["INTANGIBLE_ASSET_RETIREMENT_NOT_MONTH_END"]

    retirement_request = RetireIntangibleAssetRequest(
        org_id=organization.id,
        asset_id=acquired.asset_id,
        idempotency_key="intangible-retire",
        retirement_date=date(2026, 1, 31),
        posting_date=date(2026, 1, 31),
        gross_proceeds_fen=0,
        compensation_fen=0,
        taxes_and_fees_fen=0,
        residual_proceeds_fen=0,
        evidence_references=[retirement_evidence.id],
    )
    retired = service.retire_intangible_asset(retirement_request)
    assert retired.status == "posted", retired.errors
    assert retired.data == {
        "accumulated_amortization_fen": 1_000,
        "book_value_fen": 11_000,
        "dependency_event_ids": [str(confirmed.event_id)],
    }
    _assert_balanced(session, retired.voucher_id)
    retirement = session.scalar(
        select(IntangibleAssetRetirement).where(
            IntangibleAssetRetirement.event_id == retired.event_id
        )
    )
    assert retirement.book_value_fen == 11_000
    for event_id in (acquired.event_id, confirmed.event_id, retired.event_id):
        formal_event = session.get(BusinessEvent, event_id)
        assert formal_event.facts["accounting_rule_version"] == formal_event.rule_version
        assert formal_event.facts["accounting_rule_source_url"].startswith(
            "https://kjs.mof.gov.cn/"
        )
    state = service.get_intangible_asset(organization.id, acquired.asset_id)
    assert state.data["retired"] is True
    assert state.data["accumulated_amortization_fen"] == 1_000
    assert state.data["book_value_fen"] == 11_000

    base_service_blocked = FinanceService(session).reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=acquired.event_id,
            idempotency_key="base-reverse-intangible-acquire-blocked",
            reason="base service must preserve specialized dependency checks",
            posting_date=date(2026, 2, 1),
        )
    )
    assert base_service_blocked.errors == ["INTANGIBLE_ASSET_OPEN_DEPENDENCIES_EXIST"]
    blocked_acquisition = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=acquired.event_id,
            idempotency_key="reverse-intangible-acquire-blocked",
            reason="test reverse order",
            posting_date=date(2026, 2, 1),
        )
    )
    assert blocked_acquisition.errors == ["INTANGIBLE_ASSET_OPEN_DEPENDENCIES_EXIST"]
    blocked_amortization = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=confirmed.event_id,
            idempotency_key="reverse-intangible-amortization-blocked",
            reason="test reverse order",
            posting_date=date(2026, 2, 1),
        )
    )
    assert blocked_amortization.errors == ["INTANGIBLE_ASSET_OPEN_DEPENDENCIES_EXIST"]

    for event_id, key, posting_date in (
        (retired.event_id, "reverse-intangible-retirement", date(2026, 2, 1)),
        (confirmed.event_id, "reverse-intangible-amortization", date(2026, 2, 2)),
        (acquired.event_id, "reverse-intangible-acquisition", date(2026, 2, 3)),
    ):
        result = service.reverse_event(
            ReverseEventRequest(
                org_id=organization.id,
                event_id=event_id,
                idempotency_key=key,
                reason="test downstream-first reversal",
                posting_date=posting_date,
            )
        )
        assert result.status == "posted", result.errors

    assert session.get(BusinessEvent, acquired.event_id).status == "reversed"
    reversed_state = service.get_intangible_asset(organization.id, acquired.asset_id)
    assert reversed_state.status == "reversed"
    assert reversed_state.data["on_book"] is False
    assert reversed_state.data["accumulated_amortization_fen"] == 0
    assert reversed_state.data["book_value_fen"] == 0
    assert reversed_state.data["retired"] is False
    assert len(reversed_state.data["amortizations"]) == 1
    assert reversed_state.data["amortizations"][0]["active"] is False
    assert len(reversed_state.data["retirement_history"]) == 1
    assert reversed_state.data["retirement_history"][0]["active"] is False
    non_reusable = service.acquire_intangible_asset(
        _request(
            organization,
            evidence,
            key="intangible-code-reuse",
            asset_code="IA-LIFECYCLE",
        )
    )
    assert non_reusable.errors == ["INTANGIBLE_ASSET_CODE_ALREADY_EXISTS"]


def test_service_amortization_is_continuous_and_final_month_closes_to_zero(
    session: Session, organization: Organization
) -> None:
    evidence = _evidence(session, organization, "if")
    retirement_evidence = _evidence(session, organization, "ig")
    service = IntangibleAssetService(session)
    request = _request(
        organization,
        evidence,
        key="intangible-final-acquire",
        asset_code="IA-FINAL",
    )
    request = request.model_copy(
        update={
            "cost_components": request.cost_components.model_copy(
                update={"purchase_price_fen": 11_005}
            )
        }
    )
    acquired = service.acquire_intangible_asset(request)
    assert acquired.data["cost_fen"] == 12_005

    amounts: list[int] = []
    final_event_id = None
    for month in range(1, 13):
        posting_date = date(2026, month, monthrange(2026, month)[1])
        preview_request = PreviewIntangibleAssetAmortizationRequest(
            org_id=organization.id,
            asset_id=acquired.asset_id,
            amortization_period=f"2026-{month:02d}",
            posting_date=posting_date,
        )
        preview = service.preview_intangible_asset_amortization(preview_request)
        assert preview.status == "calculated", preview.errors
        confirmed = service.confirm_intangible_asset_amortization(
            ConfirmIntangibleAssetAmortizationRequest(
                **preview_request.model_dump(),
                idempotency_key=f"intangible-final-amortization-{month:02d}",
                calculation_hash=preview.calculation_hash,
                confirmed_by="tester",
            )
        )
        assert confirmed.status == "posted", confirmed.errors
        _assert_balanced(session, confirmed.voucher_id)
        amounts.append(confirmed.data["amortization_fen"])
        final_event_id = confirmed.event_id

    assert amounts == [1_000] * 11 + [1_005]
    assert sum(amounts) == 12_005
    state = service.get_intangible_asset(organization.id, acquired.asset_id)
    assert state.status == "posted"
    assert len(state.data["amortizations"]) == 12
    assert state.data["accumulated_amortization_fen"] == 12_005
    assert state.data["book_value_fen"] == 0
    assert state.data["retired"] is False
    assert state.data["amortizations"][-1]["event_id"] == str(final_event_id)

    exhausted = service.preview_intangible_asset_amortization(
        PreviewIntangibleAssetAmortizationRequest(
            org_id=organization.id,
            asset_id=acquired.asset_id,
            amortization_period="2027-01",
            posting_date=date(2027, 1, 31),
        )
    )
    assert exhausted.errors == ["INTANGIBLE_ASSET_AMORTIZATION_OUT_OF_SEQUENCE"]

    retired = service.retire_intangible_asset(
        RetireIntangibleAssetRequest(
            org_id=organization.id,
            asset_id=acquired.asset_id,
            idempotency_key="intangible-final-retirement",
            retirement_date=date(2026, 12, 31),
            posting_date=date(2026, 12, 31),
            gross_proceeds_fen=0,
            compensation_fen=0,
            taxes_and_fees_fen=0,
            residual_proceeds_fen=0,
            evidence_references=[retirement_evidence.id],
        )
    )
    assert retired.status == "posted", retired.errors
    assert retired.data["book_value_fen"] == 0
    _assert_balanced(session, retired.voucher_id)
