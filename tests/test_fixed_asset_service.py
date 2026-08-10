from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.fixed_asset_service import FixedAssetService
from ai_accounting.models import (
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Evidence,
    FixedAsset,
    FixedAssetActivation,
    FixedAssetDepreciation,
    FixedAssetDisposal,
    OpenItem,
    Organization,
    Voucher,
    VoucherLine,
)
from ai_accounting.schemas import (
    AcquireFixedAssetRequest,
    ActivateFixedAssetRequest,
    ConfirmFixedAssetDepreciationRequest,
    DisposeFixedAssetRequest,
    PreviewFixedAssetDepreciationRequest,
    RecordEventRequest,
    ReverseEventRequest,
)
from ai_accounting.tax import calculate_tax_period


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


def _acquisition_request(
    organization: Organization, evidence: Evidence, *, key: str = "asset-acquire-1"
) -> AcquireFixedAssetRequest:
    return AcquireFixedAssetRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "asset_code": "FA-001",
            "asset_name": "测试设备",
            "category": "production_equipment",
            "expected_use_over_one_year": True,
            "purchase_date": "2026-01-02",
            "posting_date": "2026-01-02",
            "cost_components": {
                "purchase_price_fen": 1_000_000,
                "noncreditable_tax_fen": 30_000,
                "transport_and_handling_fen": 10_000,
                "installation_and_direct_cost_fen": 10_000,
            },
            "supplier": {"kind": "supplier", "name": "设备供应商"},
            "settlement_method": "payable",
            "due_date": "2026-02-02",
            "evidence_references": [evidence.id],
            "claims_creditable_input_vat": False,
        }
    )


def _assert_balanced(session: Session, voucher_id: object) -> None:
    lines = session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == voucher_id)).all()
    assert sum(line.debit_fen for line in lines) == sum(line.credit_fen for line in lines)


def _bank_row(
    session: Session,
    organization: Organization,
    *,
    amount_fen: int,
    booking_date: date,
    seed: str,
) -> BankTransaction:
    row = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint=(seed * 64)[:64],
        booking_date=booking_date,
        amount_fen=amount_fen,
        currency="CNY",
        memo=seed,
        source_sha256=(f"s{seed}" * 64)[:64],
    )
    session.add(row)
    session.flush()
    return row


def test_acquire_and_activate_fixed_asset_are_normalized_balanced_and_idempotent(
    session: Session, organization: Organization
) -> None:
    evidence = _evidence(session, organization, "a")
    service = FixedAssetService(session)
    request = _acquisition_request(organization, evidence)

    acquired = service.acquire_fixed_asset(request)

    assert acquired.status == "posted"
    assert acquired.data["cost_fen"] == 1_050_000
    _assert_balanced(session, acquired.voucher_id)
    asset = session.get(FixedAsset, acquired.asset_id)
    assert asset.acquisition_event_id == acquired.event_id
    assert asset.cost_fen == 1_050_000
    payable = session.scalar(select(OpenItem).where(OpenItem.source_event_id == acquired.event_id))
    assert payable.original_amount_fen == 1_050_000
    assert payable.item_type == "payable"

    replay = service.acquire_fixed_asset(request)
    assert replay.status == "posted"
    assert replay.event_id == acquired.event_id
    assert replay.data["idempotent_replay"] is True
    session.flush()
    session.expire_all()
    reloaded_replay = service.acquire_fixed_asset(request)
    assert reloaded_replay.asset_id == acquired.asset_id
    assert reloaded_replay.data["cost_fen"] == 1_050_000
    changed = request.model_copy(update={"asset_name": "另一名称"})
    conflict = service.acquire_fixed_asset(changed)
    assert conflict.errors == ["FIXED_ASSET_IDEMPOTENCY_PAYLOAD_MISMATCH"]

    activation_evidence = _evidence(session, organization, "b")
    activated = service.activate_fixed_asset(
        ActivateFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
                "asset_id": asset.id,
                "idempotency_key": "asset-activate-1",
                "activation_date": "2026-01-10",
                "posting_date": "2026-01-10",
                "useful_life_months": 13,
                "residual_value_fen": 10_000,
                "benefit_area": "management",
                "evidence_references": [activation_evidence.id],
            }
        )
    )
    assert activated.status == "posted"
    _assert_balanced(session, activated.voucher_id)
    activation = session.scalar(
        select(FixedAssetActivation).where(FixedAssetActivation.event_id == activated.event_id)
    )
    assert activation.asset_id == asset.id
    assert activation.useful_life_months == 13
    assert session.get(BusinessEvent, activated.event_id).status == "posted"
    assert session.get(Voucher, activated.voucher_id).status == "posted"


def test_fixed_asset_missing_facts_and_invalid_depreciation_policy_are_stable(
    session: Session, organization: Organization
) -> None:
    service = FixedAssetService(session)
    missing = service.acquire_fixed_asset(
        AcquireFixedAssetRequest(org_id=organization.id, idempotency_key="asset-missing")
    )
    assert missing.status == "needs_information"
    assert {item.code for item in missing.missing_information} >= {
        "FIXED_ASSET_IDENTITY_REQUIRED",
        "FIXED_ASSET_COST_COMPONENTS_REQUIRED",
        "FIXED_ASSET_EVIDENCE_REQUIRED",
    }

    evidence = _evidence(session, organization, "c")
    acquired = service.acquire_fixed_asset(
        _acquisition_request(organization, evidence, key="asset-acquire-policy")
    )
    rejected = service.activate_fixed_asset(
        ActivateFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
                "asset_id": acquired.asset_id,
                "idempotency_key": "asset-activate-policy",
                "activation_date": date(2026, 1, 10),
                "posting_date": date(2026, 1, 10),
                "useful_life_months": 1_100_000,
                "residual_value_fen": 0,
                "benefit_area": "management",
                "evidence_references": [evidence.id],
            }
        )
    )
    assert rejected.status == "rejected"
    assert rejected.errors == ["FIXED_ASSET_INVALID_DEPRECIATION_POLICY"]


def test_bank_acquisition_and_retirement_match_exact_bank_rows(
    session: Session, organization: Organization
) -> None:
    service = FixedAssetService(session)
    evidence = _evidence(session, organization, "f")
    acquisition_bank = _bank_row(
        session,
        organization,
        amount_fen=-1_050_000,
        booking_date=date(2026, 1, 2),
        seed="bank-acquire",
    )
    request_data = _acquisition_request(
        organization, evidence, key="asset-bank-acquire"
    ).model_dump()
    request_data.update(
        {
            "asset_code": "FA-BANK",
            "settlement_method": "bank",
            "payment_date": date(2026, 1, 2),
            "due_date": None,
            "bank_transaction_references": [{"id": acquisition_bank.id}],
        }
    )
    acquired = service.acquire_fixed_asset(AcquireFixedAssetRequest.model_validate(request_data))
    assert acquired.status == "posted", acquired.errors
    match = session.scalar(
        select(BankTransactionMatch).where(
            BankTransactionMatch.bank_transaction_id == acquisition_bank.id
        )
    )
    assert match.event_id == acquired.event_id
    activation = service.activate_fixed_asset(
        ActivateFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
                "asset_id": acquired.asset_id,
                "idempotency_key": "asset-bank-activate",
                "activation_date": "2026-01-10",
                "posting_date": "2026-01-10",
                "useful_life_months": 13,
                "residual_value_fen": 10_000,
                "benefit_area": "service_delivery",
                "evidence_references": [evidence.id],
            }
        )
    )
    assert activation.status == "posted"
    clearance_bank = _bank_row(
        session,
        organization,
        amount_fen=-5_000,
        booking_date=date(2026, 1, 20),
        seed="retirement-cost",
    )
    retired = service.dispose_fixed_asset(
        DisposeFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
                "asset_id": acquired.asset_id,
                "idempotency_key": "asset-retire",
                "disposal_date": "2026-01-20",
                "posting_date": "2026-01-20",
                "disposal_kind": "retirement",
                "settlement_method": "none",
                "clearance_cost_fen": 5_000,
                "evidence_references": [evidence.id],
                "bank_transaction_references": [{"id": clearance_bank.id}],
            }
        )
    )
    assert retired.status == "posted", retired.errors
    assert retired.data["loss_fen"] == 1_055_000
    _assert_balanced(session, retired.voucher_id)


def test_settled_acquisition_payable_uses_common_reversal_dependency_error(
    session: Session, organization: Organization
) -> None:
    service = FixedAssetService(session)
    evidence = _evidence(session, organization, "h")
    acquired = service.acquire_fixed_asset(
        _acquisition_request(organization, evidence, key="asset-payable-acquire")
    )
    payable = session.scalar(
        select(OpenItem).where(OpenItem.source_event_id == acquired.event_id)
    )
    bank = _bank_row(
        session,
        organization,
        amount_fen=-payable.original_amount_fen,
        booking_date=date(2026, 2, 2),
        seed="supplier-payment",
    )
    payment = service.record_event(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "asset-supplier-payment",
                "event_type": "supplier_payment",
                "business_dates": {
                    "business_date": "2026-02-02",
                    "payment_date": "2026-02-02",
                    "posting_date": "2026-02-02",
                },
                "counterparty": {"id": payable.counterparty_id},
                "amounts": {"amount_fen": payable.original_amount_fen},
                "bank_transaction_references": [{"id": bank.id}],
                "allocations": [
                    {
                        "open_item_id": payable.id,
                        "amount_fen": payable.original_amount_fen,
                    }
                ],
            }
        )
    )
    assert payment.status == "posted"
    blocked = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=acquired.event_id,
            idempotency_key="reverse-settled-asset-acquisition",
            reason="测试已核销应付依赖",
            posting_date=date(2026, 2, 3),
        )
    )
    assert blocked.errors == ["REVERSE_SETTLEMENT_EVENTS_BEFORE_SOURCE_EVENT"]


def test_fixed_asset_posting_dates_follow_lifecycle_order(
    session: Session, organization: Organization
) -> None:
    service = FixedAssetService(session)
    evidence = _evidence(session, organization, "i")
    acquisition_request = _acquisition_request(
        organization, evidence, key="asset-posting-order-acquire"
    ).model_copy(update={"asset_code": "FA-ORDER", "posting_date": date(2026, 1, 15)})
    acquired = service.acquire_fixed_asset(acquisition_request)
    early_activation_request = ActivateFixedAssetRequest.model_validate(
        {
            "org_id": organization.id,
            "asset_id": acquired.asset_id,
            "idempotency_key": "asset-posting-order-early-activation",
            "activation_date": "2026-01-10",
            "posting_date": "2026-01-10",
            "useful_life_months": 13,
            "residual_value_fen": 10_000,
            "benefit_area": "management",
            "evidence_references": [evidence.id],
        }
    )
    early_activation = service.activate_fixed_asset(early_activation_request)
    assert early_activation.errors == ["FIXED_ASSET_POSTING_DATE_OUT_OF_SEQUENCE"]
    activation_request = early_activation_request.model_copy(
        update={
            "idempotency_key": "asset-posting-order-activation",
            "posting_date": date(2026, 2, 20),
        }
    )
    activated = service.activate_fixed_asset(activation_request)
    assert activated.status == "posted"
    early_depreciation = service.preview_fixed_asset_depreciation(
        PreviewFixedAssetDepreciationRequest(
            org_id=organization.id,
            asset_id=acquired.asset_id,
            depreciation_period="2026-02",
            posting_date=date(2026, 2, 10),
        )
    )
    assert early_depreciation.errors == ["FIXED_ASSET_POSTING_DATE_OUT_OF_SEQUENCE"]
    early_disposal = service.dispose_fixed_asset(
        DisposeFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
                "asset_id": acquired.asset_id,
                "idempotency_key": "asset-posting-order-disposal",
                "disposal_date": "2026-02-15",
                "posting_date": "2026-02-15",
                "disposal_kind": "retirement",
                "settlement_method": "none",
                "clearance_cost_fen": 0,
                "evidence_references": [evidence.id],
            }
        )
    )
    assert early_disposal.errors == ["FIXED_ASSET_POSTING_DATE_OUT_OF_SEQUENCE"]


def test_depreciation_preview_confirm_hash_and_sequence(
    session: Session, organization: Organization
) -> None:
    evidence = _evidence(session, organization, "d")
    service = FixedAssetService(session)
    acquired = service.acquire_fixed_asset(
        _acquisition_request(organization, evidence, key="asset-depreciation-acquire")
    )
    activated = service.activate_fixed_asset(
        ActivateFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
                "asset_id": acquired.asset_id,
                "idempotency_key": "asset-depreciation-activate",
                "activation_date": "2026-01-10",
                "posting_date": "2026-01-10",
                "useful_life_months": 13,
                "residual_value_fen": 10_000,
                "benefit_area": "management",
                "evidence_references": [evidence.id],
            }
        )
    )
    assert activated.status == "posted"
    preview_request = PreviewFixedAssetDepreciationRequest(
        org_id=organization.id,
        asset_id=acquired.asset_id,
        depreciation_period="2026-02",
        posting_date=date(2026, 2, 28),
    )
    wrong_period = service.preview_fixed_asset_depreciation(
        preview_request.model_copy(update={"posting_date": date(2026, 3, 1)})
    )
    assert wrong_period.errors == ["FIXED_ASSET_DEPRECIATION_PERIOD_INVALID"]
    preview = service.preview_fixed_asset_depreciation(preview_request)
    assert preview.status == "calculated"
    assert preview.data["depreciation_fen"] == 80_000
    confirmed = service.confirm_fixed_asset_depreciation(
        ConfirmFixedAssetDepreciationRequest(
            **preview_request.model_dump(),
            idempotency_key="asset-depreciation-confirm",
            calculation_hash=preview.calculation_hash,
            confirmed_by="tester",
        )
    )
    assert confirmed.status == "posted", confirmed.errors
    assert confirmed.data["accumulated_after_fen"] == 80_000
    _assert_balanced(session, confirmed.voucher_id)
    session.flush()
    session.expire_all()
    confirmed_replay = service.confirm_fixed_asset_depreciation(
        ConfirmFixedAssetDepreciationRequest(
            **preview_request.model_dump(),
            idempotency_key="asset-depreciation-confirm",
            calculation_hash=preview.calculation_hash,
            confirmed_by="tester",
        )
    )
    assert confirmed_replay.calculation_hash == preview.calculation_hash
    assert confirmed_replay.data["accumulated_after_fen"] == 80_000
    duplicate = service.preview_fixed_asset_depreciation(preview_request)
    assert duplicate.errors == ["FIXED_ASSET_DEPRECIATION_ALREADY_POSTED"]
    skipped = service.preview_fixed_asset_depreciation(
        preview_request.model_copy(
            update={"depreciation_period": "2026-04", "posting_date": date(2026, 4, 30)}
        )
    )
    assert skipped.errors == ["FIXED_ASSET_DEPRECIATION_OUT_OF_SEQUENCE"]

    disposal_evidence = _evidence(session, organization, "e")
    premature = service.dispose_fixed_asset(
        DisposeFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
                "asset_id": acquired.asset_id,
                "idempotency_key": "asset-disposal-premature",
                "disposal_date": "2026-03-15",
                "posting_date": "2026-03-15",
                "disposal_kind": "sale",
                "gross_proceeds_fen": 500_000,
                "invoice_type": "ordinary",
                "waive_exemption": False,
                "settlement_method": "receivable",
                "customer": {"kind": "customer", "name": "资产客户"},
                "tax_obligation_date": "2026-03-15",
                "clearance_cost_fen": 0,
                "evidence_references": [disposal_evidence.id],
            }
        )
    )
    assert premature.errors == ["FIXED_ASSET_DISPOSAL_WITH_UNPOSTED_DEPRECIATION"]

    disposed = service.dispose_fixed_asset(
        DisposeFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
                "asset_id": acquired.asset_id,
                "idempotency_key": "asset-disposal-sale",
                "disposal_date": "2026-02-28",
                "posting_date": "2026-02-28",
                "disposal_kind": "sale",
                "gross_proceeds_fen": 500_000,
                "invoice_type": "ordinary",
                "waive_exemption": False,
                "settlement_method": "receivable",
                "customer": {"kind": "customer", "name": "资产客户"},
                "tax_obligation_date": "2026-02-28",
                "clearance_cost_fen": 0,
                "evidence_references": [disposal_evidence.id],
            }
        )
    )
    assert disposed.status == "posted", disposed.errors
    assert disposed.data["vat_tax_sales_fen"] == 485_437
    assert disposed.data["vat_fen"] == 9_709
    assert disposed.data["book_value_fen"] == 970_000
    _assert_balanced(session, disposed.voucher_id)
    disposal = session.scalar(
        select(FixedAssetDisposal).where(FixedAssetDisposal.event_id == disposed.event_id)
    )
    assert disposal.activation_id == session.scalar(
        select(FixedAssetActivation.id).where(
            FixedAssetActivation.event_id == activated.event_id
        )
    )
    receivable = session.scalar(
        select(OpenItem).where(OpenItem.source_event_id == disposed.event_id)
    )
    assert receivable.item_type == "receivable"
    assert receivable.original_amount_fen == 500_000
    session.flush()
    session.expire_all()
    disposed_replay = service.dispose_fixed_asset(
        DisposeFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
                "asset_id": acquired.asset_id,
                "idempotency_key": "asset-disposal-sale",
                "disposal_date": "2026-02-28",
                "posting_date": "2026-02-28",
                "disposal_kind": "sale",
                "gross_proceeds_fen": 500_000,
                "invoice_type": "ordinary",
                "waive_exemption": False,
                "settlement_method": "receivable",
                "customer": {"kind": "customer", "name": "资产客户"},
                "tax_obligation_date": "2026-02-28",
                "clearance_cost_fen": 0,
                "evidence_references": [disposal_evidence.id],
            }
        )
    )
    assert disposed_replay.event_id == disposed.event_id
    assert disposed_replay.data["vat_fen"] == 9_709
    projected = service.get_fixed_asset(organization.id, acquired.asset_id)
    assert projected.data["state"] == "disposed"
    assert projected.data["disposal"]["activation_id"] == str(disposal.activation_id)

    blocked_activation = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=activated.event_id,
            idempotency_key="reverse-activation-blocked",
            reason="应先冲正下游",
            posting_date=date(2026, 3, 1),
        )
    )
    assert blocked_activation.errors == ["FIXED_ASSET_OPEN_DEPENDENCIES_EXIST"]
    reversed_disposal = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=disposed.event_id,
            idempotency_key="reverse-disposal",
            reason="测试逆序冲正",
            posting_date=date(2026, 3, 1),
        )
    )
    assert reversed_disposal.status == "posted"
    blocked_by_depreciation = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=activated.event_id,
            idempotency_key="reverse-activation-blocked-by-depreciation",
            reason="应先冲正折旧",
            posting_date=date(2026, 3, 1),
        )
    )
    assert blocked_by_depreciation.errors == ["FIXED_ASSET_OPEN_DEPENDENCIES_EXIST"]
    reversed_depreciation = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=confirmed.event_id,
            idempotency_key="reverse-depreciation",
            reason="重设折旧政策",
            posting_date=date(2026, 3, 1),
        )
    )
    assert reversed_depreciation.status == "posted"
    reversed_activation = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=activated.event_id,
            idempotency_key="reverse-activation",
            reason="重设受益区域",
            posting_date=date(2026, 3, 1),
        )
    )
    assert reversed_activation.status == "posted"

    activation_b = service.activate_fixed_asset(
        ActivateFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
                "asset_id": acquired.asset_id,
                "idempotency_key": "asset-reactivate",
                "activation_date": "2026-01-20",
                "posting_date": "2026-02-20",
                "useful_life_months": 26,
                "residual_value_fen": 10_000,
                "benefit_area": "sales",
                "evidence_references": [evidence.id],
            }
        )
    )
    assert activation_b.status == "posted"
    preview_b_request = PreviewFixedAssetDepreciationRequest(
        org_id=organization.id,
        asset_id=acquired.asset_id,
        depreciation_period="2026-02",
        posting_date=date(2026, 2, 28),
    )
    preview_b = service.preview_fixed_asset_depreciation(preview_b_request)
    confirmed_b = service.confirm_fixed_asset_depreciation(
        ConfirmFixedAssetDepreciationRequest(
            **preview_b_request.model_dump(),
            idempotency_key="asset-depreciation-confirm-b",
            calculation_hash=preview_b.calculation_hash,
            confirmed_by="tester",
        )
    )
    assert confirmed_b.status == "posted", confirmed_b.errors
    old_fact = session.scalar(
        select(FixedAssetDepreciation).where(
            FixedAssetDepreciation.event_id == confirmed.event_id
        )
    )
    new_fact = session.scalar(
        select(FixedAssetDepreciation).where(
            FixedAssetDepreciation.event_id == confirmed_b.event_id
        )
    )
    assert old_fact.activation_id != new_fact.activation_id
    assert old_fact.activation_id == disposal.activation_id
    assert new_fact.activation_id == session.scalar(
        select(FixedAssetActivation.id).where(
            FixedAssetActivation.event_id == activation_b.event_id
        )
    )
    projected_b = service.get_fixed_asset(organization.id, acquired.asset_id)
    assert projected_b.data["activation"]["benefit_area"] == "sales"
    assert projected_b.data["depreciations"][0]["activation_id"] == str(
        new_fact.activation_id
    )
    assert len(projected_b.data["activation_history"]) == 2
    assert len(projected_b.data["depreciation_history"]) == 2
    assert len(projected_b.data["disposal_history"]) == 1
    assert projected_b.data["activation_history"][0]["event"]["status"] == "reversed"
    assert projected_b.data["depreciation_history"][0]["event"]["status"] == "reversed"
    assert projected_b.data["disposal_history"][0]["event"]["status"] == "reversed"
    assert projected_b.data["disposal_history"][0]["event"]["voucher"]["id"]
    assert projected_b.data["asset"]["event"]["evidence_ids"] == [str(evidence.id)]

    asset_before_final_reversal = (
        session.get(FixedAsset, acquired.asset_id).asset_code,
        session.get(FixedAsset, acquired.asset_id).cost_fen,
        session.get(FixedAsset, acquired.asset_id).acquisition_event_id,
    )
    acquisition_voucher = session.scalar(
        select(Voucher).where(Voucher.event_id == acquired.event_id)
    )
    acquisition_lines_before = [
        (line.line_number, line.account_id, line.debit_fen, line.credit_fen)
        for line in session.scalars(
            select(VoucherLine)
            .where(VoucherLine.voucher_id == acquisition_voucher.id)
            .order_by(VoucherLine.line_number)
        ).all()
    ]
    for source_event_id, key in (
        (confirmed_b.event_id, "reverse-depreciation-b"),
        (activation_b.event_id, "reverse-activation-b"),
        (acquired.event_id, "reverse-acquisition"),
    ):
        reversed_result = service.reverse_event(
            ReverseEventRequest(
                org_id=organization.id,
                event_id=source_event_id,
                idempotency_key=key,
                reason="完成完整逆序冲正覆盖",
                posting_date=date(2026, 3, 2),
            )
        )
        assert reversed_result.status == "posted", reversed_result.errors
    session.flush()
    session.expire_all()
    reversed_card = service.get_fixed_asset(organization.id, acquired.asset_id)
    assert reversed_card.status == "reversed"
    assert reversed_card.data["state"] == "reversed"
    asset_after_final_reversal = session.get(FixedAsset, acquired.asset_id)
    assert (
        asset_after_final_reversal.asset_code,
        asset_after_final_reversal.cost_fen,
        asset_after_final_reversal.acquisition_event_id,
    ) == asset_before_final_reversal
    acquisition_lines_after = [
        (line.line_number, line.account_id, line.debit_fen, line.credit_fen)
        for line in session.scalars(
            select(VoucherLine)
            .where(VoucherLine.voucher_id == acquisition_voucher.id)
            .order_by(VoucherLine.line_number)
        ).all()
    ]
    assert acquisition_lines_after == acquisition_lines_before
    assert session.get(FixedAssetDepreciation, old_fact.id).activation_id == old_fact.activation_id
    assert session.get(FixedAssetDepreciation, new_fact.id).activation_id == new_fact.activation_id


def test_fixed_asset_sale_flows_into_period_tax_relief_and_special_invoice_exclusion(
    session: Session, organization: Organization
) -> None:
    service = FixedAssetService(session)
    evidence = _evidence(session, organization, "g")

    def post_sale(asset_code: str, gross_fen: int, invoice_type: str) -> object:
        acquisition_request = _acquisition_request(
            organization, evidence, key=f"tax-acquire-{asset_code}"
        ).model_copy(update={"asset_code": asset_code})
        acquired = service.acquire_fixed_asset(acquisition_request)
        activated = service.activate_fixed_asset(
            ActivateFixedAssetRequest.model_validate(
                {
                    "org_id": organization.id,
                    "asset_id": acquired.asset_id,
                    "idempotency_key": f"tax-activate-{asset_code}",
                    "activation_date": "2026-01-10",
                    "posting_date": "2026-01-10",
                    "useful_life_months": 13,
                    "residual_value_fen": 10_000,
                    "benefit_area": "management",
                    "evidence_references": [evidence.id],
                }
            )
        )
        assert activated.status == "posted"
        return service.dispose_fixed_asset(
            DisposeFixedAssetRequest.model_validate(
                {
                    "org_id": organization.id,
                    "asset_id": acquired.asset_id,
                    "idempotency_key": f"tax-dispose-{asset_code}",
                    "disposal_date": "2026-01-20",
                    "posting_date": "2026-01-20",
                    "disposal_kind": "sale",
                    "gross_proceeds_fen": gross_fen,
                    "invoice_type": invoice_type,
                    "waive_exemption": False,
                    "settlement_method": "receivable",
                    "customer": {"kind": "customer", "name": f"客户-{asset_code}"},
                    "tax_obligation_date": "2026-01-20",
                    "clearance_cost_fen": 0,
                    "evidence_references": [evidence.id],
                }
            )
        )

    ordinary = post_sale("FA-TAX-ORDINARY", 500_000, "ordinary")
    assert ordinary.status == "posted"
    ordinary_period = calculate_tax_period(
        session, organization, date(2026, 1, 1), date(2026, 3, 31)
    )
    assert ordinary_period.gross_sales_fen == 500_000
    assert ordinary_period.net_sales_fen == 485_437
    assert ordinary_period.vat_accrued_fen == 9_709
    assert ordinary_period.vat_relief_fen == 9_709

    special = post_sale("FA-TAX-SPECIAL", 100_000, "special")
    assert special.status == "posted"
    combined = calculate_tax_period(
        session, organization, date(2026, 1, 1), date(2026, 3, 31)
    )
    assert combined.gross_sales_fen == 600_000
    assert combined.net_sales_fen == 582_524
    assert combined.vat_accrued_fen == 11_651
    assert combined.vat_relief_fen == 9_709
    assert combined.vat_payable_fen == 1_942
