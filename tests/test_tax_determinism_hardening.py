from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_accounting.fixed_asset_service import FixedAssetService
from ai_accounting.models import (
    BusinessEvent,
    Evidence,
    FixedAssetDisposal,
    Organization,
    TaxPeriod,
    TaxPeriodSource,
    TaxRule,
    Voucher,
)
from ai_accounting.schemas import (
    AcquireFixedAssetRequest,
    ActivateFixedAssetRequest,
    DisposeFixedAssetRequest,
    RecordEventRequest,
    ReverseEventRequest,
    TaxPeriodConfirmRequest,
    TaxPeriodPreviewRequest,
)
from ai_accounting.service import FinanceService

Q1_START = date(2026, 1, 1)
Q1_END = date(2026, 3, 31)


def _explicit_tax_facts(*, invoice_type: str = "special") -> dict[str, object]:
    return {
        "taxable": True,
        "rate_percent": "1",
        "invoice_type": invoice_type,
        "waive_exemption": False,
        "tax_due_on_event": True,
    }


def _sale_request(
    organization: Organization,
    *,
    key: str,
    business_date: date = date(2026, 1, 15),
    gross_fen: int = 10_100,
) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "event_type": "service_cash_sale",
            "business_dates": {
                "business_date": business_date,
                "fulfillment_date": business_date,
                "payment_date": business_date,
                "tax_obligation_date": business_date,
                "posting_date": business_date,
            },
            "amounts": {"gross_amount_fen": gross_fen},
            "tax_facts": _explicit_tax_facts(),
        }
    )


def _preview(
    service: FinanceService,
    organization: Organization,
    start_date: date = Q1_START,
    end_date: date = Q1_END,
) -> dict[str, Any]:
    return service.preview_tax_period(
        TaxPeriodPreviewRequest(
            org_id=organization.id,
            start_date=start_date,
            end_date=end_date,
            adjustment_posting_date=end_date,
        )
    )


def _confirm(
    service: FinanceService,
    organization: Organization,
    calculation_hash: str,
    *,
    key: str,
    start_date: date = Q1_START,
    end_date: date = Q1_END,
):
    return service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            org_id=organization.id,
            start_date=start_date,
            end_date=end_date,
            adjustment_posting_date=end_date,
            calculation_hash=calculation_hash,
            idempotency_key=key,
        )
    )


def _leaf_paths(value: Any, prefix: tuple[str | int, ...] = ()) -> list[tuple[str | int, ...]]:
    if isinstance(value, dict):
        return [
            path
            for key in sorted(value)
            for path in _leaf_paths(value[key], (*prefix, key))
        ]
    if isinstance(value, list):
        return [
            path
            for index, child in enumerate(value)
            for path in _leaf_paths(child, (*prefix, index))
        ]
    return [prefix]


def _mutate_leaf(payload: dict[str, Any], path: tuple[str | int, ...]) -> dict[str, Any]:
    mutated = deepcopy(payload)
    parent: Any = mutated
    for part in path[:-1]:
        parent = parent[part]
    leaf = path[-1]
    current = parent[leaf]
    if isinstance(current, bool):
        replacement: Any = not current
    elif isinstance(current, int):
        replacement = current + 1
    elif current is None:
        replacement = "tampered"
    else:
        replacement = f"{current}#tampered"
    parent[leaf] = replacement
    return mutated


def _canonical_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _count(session: Session, model: type[object]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _evidence(session: Session, organization: Organization, seed: str) -> Evidence:
    evidence = Evidence(
        org_id=organization.id,
        sha256=(seed * 64)[:64],
        original_name=f"hardening-{seed}.pdf",
        media_type="application/pdf",
        source="hardening-test",
        size_bytes=1,
        storage_path=f"hardening/{seed}",
    )
    session.add(evidence)
    session.flush()
    return evidence


def _active_asset(
    service: FixedAssetService,
    organization: Organization,
    evidence: Evidence,
    *,
    asset_code: str,
) -> uuid.UUID:
    acquired = service.acquire_fixed_asset(
        AcquireFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": f"hardening-acquire-{asset_code}",
                "asset_code": asset_code,
                "asset_name": f"硬化验收资产 {asset_code}",
                "category": "production_equipment",
                "expected_use_over_one_year": True,
                "purchase_date": "2026-01-02",
                "posting_date": "2026-01-02",
                "cost_components": {
                    "purchase_price_fen": 1_000_000,
                    "noncreditable_tax_fen": 0,
                    "transport_and_handling_fen": 0,
                    "installation_and_direct_cost_fen": 0,
                },
                "supplier": {"kind": "supplier", "name": f"供应商 {asset_code}"},
                "settlement_method": "payable",
                "due_date": "2026-02-02",
                "evidence_references": [evidence.id],
                "claims_creditable_input_vat": False,
            }
        )
    )
    assert acquired.status.value == "posted", acquired.errors
    activated = service.activate_fixed_asset(
        ActivateFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
                "asset_id": acquired.asset_id,
                "idempotency_key": f"hardening-activate-{asset_code}",
                "activation_date": "2026-01-10",
                "posting_date": "2026-01-10",
                "useful_life_months": 13,
                "residual_value_fen": 10_000,
                "benefit_area": "management",
                "evidence_references": [evidence.id],
            }
        )
    )
    assert activated.status.value == "posted", activated.errors
    assert acquired.asset_id is not None
    return acquired.asset_id


def test_preview_hash_payload_is_reproducible_and_every_leaf_tamper_is_stale(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    source = service.record_event(_sale_request(organization, key="hash-payload-source"))
    assert source.status.value == "posted"
    preview = _preview(service, organization)
    assert preview["status"] == "calculated", preview

    payload_text = preview["calculation_hash_payload"]
    payload = json.loads(payload_text)
    assert set(payload) == {
        "organization",
        "period",
        "vat_rule",
        "surtax_rule",
        "source_events",
        "calculation",
    }
    assert payload["organization"] == {
        "id": str(organization.id),
        "filing_cycle": "quarterly",
        "jurisdiction": "CN",
        "urban_maintenance_rate": "0.07000",
    }
    assert payload["period"] == {
        "start_date": "2026-01-01",
        "end_date": "2026-03-31",
        "adjustment_posting_date": "2026-03-31",
    }
    assert payload["source_events"] == preview["source_event_snapshots"]
    assert _canonical_hash(payload) == preview["calculation_hash"]
    assert hashlib.sha256(payload_text.encode("utf-8")).hexdigest() == preview["calculation_hash"]

    paths = _leaf_paths(payload)
    assert len(paths) >= 35
    for index, path in enumerate(paths):
        tampered_hash = _canonical_hash(_mutate_leaf(payload, path))
        assert tampered_hash != preview["calculation_hash"]
        rejected = _confirm(
            service,
            organization,
            tampered_hash,
            key=f"tampered-hash-leaf-{index}",
        )
        assert rejected.status.value == "rejected", path
        assert rejected.errors == ["TAX_PERIOD_CALCULATION_STALE"], path

    confirmed = _confirm(
        service,
        organization,
        str(preview["calculation_hash"]),
        key="hash-payload-confirm",
    )
    assert confirmed.status.value == "posted", confirmed.errors
    period = session.get(TaxPeriod, uuid.UUID(confirmed.data["tax_period_id"]))
    assert period is not None
    assert period.calculation_hash == preview["calculation_hash"]
    assert period.calculation["calculation_hash_payload"] == payload_text
    event = session.get(BusinessEvent, confirmed.event_id)
    assert event is not None
    assert event.facts["tax_period"]["calculation_hash_payload"] == payload_text


def test_old_snapshot_reverses_after_all_organization_tax_configuration_changes(
    session: Session, organization: Organization
) -> None:
    rules = session.scalars(
        select(TaxRule).where(
            TaxRule.jurisdiction == organization.jurisdiction,
            TaxRule.code.in_(("small_scale_vat_2026_2027", "small_scale_surtax_2023_2027")),
        )
    ).all()
    assert len(rules) == 2
    replacement_jurisdiction = "CN-HARDENING"
    for rule in rules:
        session.add(
            TaxRule(
                code=rule.code,
                jurisdiction=replacement_jurisdiction,
                effective_from=rule.effective_from,
                effective_to=rule.effective_to,
                version=rule.version,
                source_url=rule.source_url,
                parameters=deepcopy(rule.parameters),
            )
        )
    session.flush()

    service = FinanceService(session)
    source = service.record_event(_sale_request(organization, key="config-snapshot-source"))
    assert source.status.value == "posted"
    old_preview = _preview(service, organization)
    confirmed = _confirm(
        service,
        organization,
        str(old_preview["calculation_hash"]),
        key="config-snapshot-confirm",
    )
    assert confirmed.status.value == "posted", confirmed.errors
    period = session.get(TaxPeriod, uuid.UUID(confirmed.data["tax_period_id"]))
    assert period is not None
    frozen_calculation = deepcopy(period.calculation)

    organization.filing_cycle = "monthly"
    organization.jurisdiction = replacement_jurisdiction
    organization.urban_maintenance_rate = Decimal("0.05")
    session.flush()
    reversed_result = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=confirmed.event_id,
            idempotency_key="config-snapshot-reverse",
            reason="组织税务配置变化后规范冲正旧快照",
            posting_date=date(2026, 4, 1),
        )
    )
    assert reversed_result.status.value == "posted", reversed_result.errors
    session.flush()
    session.expire_all()
    period = session.get(TaxPeriod, period.id)
    assert period is not None
    assert period.status == "reversed"
    assert period.calculation == frozen_calculation

    refreshed = _preview(service, organization, Q1_START, date(2026, 1, 31))
    assert refreshed["status"] == "calculated", refreshed
    assert refreshed["calculation_hash"] != old_preview["calculation_hash"]
    refreshed_payload = json.loads(refreshed["calculation_hash_payload"])
    assert refreshed_payload["organization"] == {
        "id": str(organization.id),
        "filing_cycle": "monthly",
        "jurisdiction": replacement_jurisdiction,
        "urban_maintenance_rate": "0.05000",
    }


def test_fixed_asset_sale_is_locked_by_posted_period_but_retirement_is_not(
    session: Session, organization: Organization
) -> None:
    evidence = _evidence(session, organization, "fixed-asset-source-lock")
    fixed_service = FixedAssetService(session)
    sale_asset_id = _active_asset(
        fixed_service, organization, evidence, asset_code="FA-HARDENING-SALE"
    )
    retirement_asset_id = _active_asset(
        fixed_service, organization, evidence, asset_code="FA-HARDENING-RETIREMENT"
    )
    source = fixed_service.record_event(_sale_request(organization, key="asset-lock-tax-source"))
    assert source.status.value == "posted"
    preview = _preview(fixed_service, organization)
    confirmed = _confirm(
        fixed_service,
        organization,
        str(preview["calculation_hash"]),
        key="asset-lock-period-confirm",
    )
    assert confirmed.status.value == "posted", confirmed.errors

    disposal_count = _count(session, FixedAssetDisposal)
    voucher_count = _count(session, Voucher)
    blocked_sale = fixed_service.dispose_fixed_asset(
        DisposeFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
                "asset_id": sale_asset_id,
                "idempotency_key": "fixed-asset-sale-inside-closed-period",
                "disposal_date": "2026-01-20",
                "posting_date": "2026-01-20",
                "disposal_kind": "sale",
                "gross_proceeds_fen": 500_000,
                "invoice_type": "ordinary",
                "waive_exemption": False,
                "settlement_method": "receivable",
                "customer": {"kind": "customer", "name": "硬化验收资产客户"},
                "tax_obligation_date": "2026-01-20",
                "clearance_cost_fen": 0,
                "evidence_references": [evidence.id],
            }
        )
    )
    assert blocked_sale.status.value == "rejected"
    assert blocked_sale.errors == ["TAX_PERIOD_SOURCE_LOCKED"]
    assert _count(session, FixedAssetDisposal) == disposal_count
    assert _count(session, Voucher) == voucher_count

    retired = fixed_service.dispose_fixed_asset(
        DisposeFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
                "asset_id": retirement_asset_id,
                "idempotency_key": "fixed-asset-retirement-inside-closed-period",
                "disposal_date": "2026-01-20",
                "posting_date": "2026-01-20",
                "disposal_kind": "retirement",
                "settlement_method": "none",
                "clearance_cost_fen": 0,
                "evidence_references": [evidence.id],
            }
        )
    )
    assert retired.status.value == "posted", retired.errors
    retirement = session.scalar(
        select(FixedAssetDisposal).where(FixedAssetDisposal.event_id == retired.event_id)
    )
    assert retirement is not None
    assert retirement.disposal_kind == "retirement"
    retirement_event = session.get(BusinessEvent, retired.event_id)
    assert retirement_event is not None
    assert retirement_event.tax_obligation_date is None
    assert retirement_event.facts["derived"]["taxable_gross_fen"] == 0


def test_zero_adjustment_period_has_stable_code_and_no_confirmation_side_effects(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    preview = _preview(service, organization)
    assert preview["status"] == "calculated", preview
    assert preview["source_events"] == []
    assert preview["vat_relief_fen"] == 0
    assert preview["surtax_total_fen"] == 0
    before = {
        BusinessEvent: _count(session, BusinessEvent),
        Voucher: _count(session, Voucher),
        TaxPeriod: _count(session, TaxPeriod),
        TaxPeriodSource: _count(session, TaxPeriodSource),
    }

    rejected = _confirm(
        service,
        organization,
        str(preview["calculation_hash"]),
        key="zero-adjustment-confirm",
    )
    assert rejected.status.value == "rejected"
    assert rejected.errors == ["TAX_PERIOD_NO_ADJUSTMENT"]
    assert {model: _count(session, model) for model in before} == before
