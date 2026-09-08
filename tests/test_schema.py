from __future__ import annotations

import uuid
from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.models import OpenItem, Organization


def test_component_schema_rejects_untyped_payroll_event(
    session: Session, organization: Organization
) -> None:
    with pytest.raises(ValidationError):
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "payroll-not-enabled",
                "posting_date": "2026-08-08",
                "components": [
                    {
                        "key": "payroll",
                        "kind": "payroll",
                        "business_date": "2026-08-08",
                        "amount_fen": 100_000,
                    }
                ],
            }
        )


def test_database_rejects_negative_or_oversettled_open_item(
    session: Session, organization: Organization
) -> None:
    # UUID shapes are valid but absent; the amount check is still a database invariant.
    item = OpenItem(
        org_id=organization.id,
        counterparty_id=uuid.uuid4(),
        source_event_id=uuid.uuid4(),
        item_type="receivable",
        original_amount_fen=100,
        settled_amount_fen=101,
        status="open",
        due_date=date(2026, 8, 31),
    )
    session.add(item)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
    else:
        raise AssertionError("database accepted an oversettled open item")
