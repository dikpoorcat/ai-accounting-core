"""Effective-dated organization profile lookup used by deterministic calculations."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Organization, OrganizationProfileVersion


def profile_as_of(
    session: Session,
    *,
    org_id: uuid.UUID,
    as_of: date,
) -> OrganizationProfileVersion | Organization:
    """Return the immutable profile effective on a business or report date.

    Legacy databases remain readable before the forward migration creates the
    first version.  That fallback is deliberately limited to the current
    organization projection and disappears once a version exists.
    """

    profile = session.scalar(
        select(OrganizationProfileVersion)
        .where(
            OrganizationProfileVersion.org_id == org_id,
            OrganizationProfileVersion.effective_from <= as_of,
        )
        .order_by(OrganizationProfileVersion.effective_from.desc())
        .limit(1)
    )
    if profile is not None:
        return profile
    version_exists = session.scalar(
        select(OrganizationProfileVersion.id)
        .where(OrganizationProfileVersion.org_id == org_id)
        .limit(1)
    )
    if version_exists is not None:
        raise ValueError("ORGANIZATION_PROFILE_NOT_EFFECTIVE")
    organization = session.get(Organization, org_id)
    if organization is None:
        raise ValueError("ORGANIZATION_NOT_FOUND")
    return organization
