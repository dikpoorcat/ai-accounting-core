"""Versioned management information, independent of immutable accounting facts."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .schemas import CounterpartyRef


class BusinessMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counterparty: CounterpartyRef | None = None
    beneficiary: CounterpartyRef | None = None
    handler: CounterpartyRef | None = None
    purpose: str | None = Field(default=None, max_length=2000)
    description: str | None = Field(default=None, max_length=2000)
    project_reference: str | None = Field(default=None, max_length=200)
    contract_reference: str | None = Field(default=None, max_length=500)
    acceptance_reference: str | None = Field(default=None, max_length=500)
    obligation_reference: str | None = Field(default=None, max_length=500)
    refund_reference: str | None = Field(default=None, max_length=500)
    assessment_reference: str | None = Field(default=None, max_length=500)
    declaration_reference: str | None = Field(default=None, max_length=500)
    confirmation_note: str | None = Field(default=None, max_length=2000)
    capitalization_basis: str | None = Field(default=None, max_length=2000)
    reason: str | None = Field(default=None, max_length=2000)
    reason_code: str | None = Field(default=None, max_length=100)
    asset_code: str | None = Field(default=None, max_length=100)
    asset_name: str | None = Field(default=None, max_length=200)
    contract_name: str | None = Field(default=None, max_length=200)
    borrowing_code: str | None = Field(default=None, max_length=100)
    rights_description: str | None = Field(default=None, max_length=2000)
    life_basis_explanation: str | None = Field(default=None, max_length=2000)
    due_date: date | None = None
    advance_payment_date: date | None = None
    withholding_agency_code: str | None = Field(default=None, max_length=100)
    withholding_agency_name: str | None = Field(default=None, max_length=200)
    other_right_type_description: str | None = Field(default=None, max_length=500)
    interest_due_dates: list[date] | None = None


class BusinessReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_key: str = Field(min_length=1, max_length=200)
    component_key: str = Field(min_length=1, max_length=100)


class UpdateBusinessMetadataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    source: BusinessReference
    metadata: BusinessMetadata
    expected_version: int = Field(ge=0, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=200)


def validate_metadata(session, org_id, metadata):
    from .models import Counterparty

    for name in ("counterparty", "beneficiary", "handler"):
        reference = getattr(metadata, name)
        if reference and reference.id:
            party = session.get(Counterparty, reference.id)
            if party is None or party.org_id != org_id:
                raise ValueError("METADATA_COUNTERPARTY_NOT_IN_COMPANY")


def metadata_history(session, org_id, event_id, component_key):
    from .models import BusinessMetadataVersion

    return session.scalars(
        select(BusinessMetadataVersion)
        .where(
            BusinessMetadataVersion.org_id == org_id,
            BusinessMetadataVersion.event_id == event_id,
            BusinessMetadataVersion.component_key == component_key,
        )
        .order_by(BusinessMetadataVersion.version)
    ).all()


def metadata_projection(session, org_id, event_id, component_key):
    from .models import Counterparty

    history = metadata_history(session, org_id, event_id, component_key)
    values = history[-1].metadata_values if history else {}
    display_names = {}
    for field in ("counterparty", "beneficiary", "handler"):
        reference = values.get(field) or {}
        if reference.get("id"):
            party = session.get(Counterparty, uuid.UUID(str(reference["id"])))
            if party is not None and party.org_id == org_id:
                display_names[field] = party.name
        elif reference.get("name"):
            display_names[field] = reference["name"]
    return {
        "version": history[-1].version if history else 0,
        "metadata": values,
        "display_names": display_names,
        "history": [
            {
                "version": row.version,
                "metadata": row.metadata_values,
                "created_at": row.created_at.isoformat(),
                "execution_attribution_id": str(row.execution_attribution_id)
                if row.execution_attribution_id
                else None,
            }
            for row in history
        ],
    }


def event_metadata_projection(session, org_id, event_id):
    """Include stable keys removed by later amendments or whole-event deletion."""
    from .models import BusinessMetadataVersion

    keys = session.scalars(
        select(BusinessMetadataVersion.component_key)
        .where(
            BusinessMetadataVersion.org_id == org_id,
            BusinessMetadataVersion.event_id == event_id,
        )
        .distinct()
        .order_by(BusinessMetadataVersion.component_key)
    ).all()
    return {key: metadata_projection(session, org_id, event_id, key) for key in keys}


def save_initial_metadata(session, event, component):
    from .models import BusinessMetadataVersion

    values = component.metadata.model_dump(mode="json", exclude_none=True)
    if not values:
        return
    validate_metadata(session, event.org_id, component.metadata)
    if metadata_history(session, event.org_id, event.id, component.key):
        return
    session.add(
        BusinessMetadataVersion(
            org_id=event.org_id,
            event_id=event.id,
            component_key=component.key,
            version=1,
            metadata_values=values,
            idempotency_key=f"initial:{event.id}:{component.key}",
            request_hash=hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(),
        )
    )


def update_business_metadata(session, request):
    from .models import AuditLog, BusinessEvent, BusinessEventComponent, BusinessMetadataVersion

    payload = request.model_dump(mode="json")
    payload["metadata"] = request.metadata.model_dump(mode="json", exclude_unset=True)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    try:
        with session.begin_nested():
            event = session.scalar(
                select(BusinessEvent)
                .where(
                    BusinessEvent.org_id == request.org_id,
                    BusinessEvent.idempotency_key == request.source.event_key,
                )
                .with_for_update()
            )
            if event is None:
                raise ValueError("METADATA_BUSINESS_NOT_FOUND")
            previous = session.scalar(
                select(BusinessMetadataVersion).where(
                    BusinessMetadataVersion.org_id == request.org_id,
                    BusinessMetadataVersion.idempotency_key == request.idempotency_key,
                )
            )
            if previous:
                if previous.request_hash != digest:
                    raise ValueError("IDEMPOTENCY_KEY_PAYLOAD_MISMATCH")
                return {
                    "status": "updated",
                    "version": previous.version,
                    "metadata": previous.metadata_values,
                    "idempotent_replay": True,
                }
            component = session.scalar(
                select(BusinessEventComponent).where(
                    BusinessEventComponent.event_id == event.id,
                    BusinessEventComponent.key == request.source.component_key,
                )
            )
            if component is None or event.status not in {"posted", "reversed"}:
                raise ValueError("METADATA_BUSINESS_NOT_ACTIVE")
            validate_metadata(session, request.org_id, request.metadata)
            current = metadata_projection(session, request.org_id, event.id, component.key)
            if current["version"] != request.expected_version:
                raise ValueError("METADATA_VERSION_CONFLICT")
            values = current["metadata"] | request.metadata.model_dump(
                mode="json", exclude_unset=True
            )
            values = {key: value for key, value in values.items() if value is not None}
            version = current["version"] + 1
            session.add(
                BusinessMetadataVersion(
                    org_id=request.org_id,
                    event_id=event.id,
                    component_key=component.key,
                    version=version,
                    metadata_values=values,
                    idempotency_key=request.idempotency_key,
                    request_hash=digest,
                )
            )
            session.add(
                AuditLog(
                    org_id=request.org_id,
                    event_id=event.id,
                    action="business_metadata_updated",
                    actor="ai_agent:ai-accounting-core",
                    details={
                        "component_key": component.key,
                        "version": version,
                        "previous_version": current["version"],
                    },
                )
            )
            session.flush()
            return {"status": "updated", "version": version, "metadata": values}
    except ValueError as exc:
        return {"status": "rejected", "errors": [str(exc)]}
    except IntegrityError:
        return {"status": "rejected", "errors": ["METADATA_CONCURRENT_CONFLICT"]}
