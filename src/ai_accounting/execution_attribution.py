"""Persist server-owned execution identity for authenticated business writes."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

from .identity import ExecutionContext
from .models import (
    EXECUTION_ATTRIBUTION_SESSION_KEY,
    ExecutionAttribution,
)


@contextmanager
def persist_execution_attribution(
    session: Session,
    *,
    context: ExecutionContext,
    tool_name: str,
) -> Iterator[ExecutionAttribution]:
    """Create one immutable call record and bind new roots to it.

    The row is intentionally created even when the deterministic workflow later
    returns ``needs_information``, ``rejected``, or an idempotent replay.  It has
    no caller-provided actor or executor fields.
    """

    attribution = ExecutionAttribution(
        id=uuid.uuid4(),
        org_id=context.org_id,
        catalog_instance_id=context.catalog_instance_id,
        owner_account_id=context.owner_account_id,
        owner_session_id=context.owner_session_id,
        owner_credential_version=context.owner_credential_version,
        executor_kind=context.executor_kind.value,
        executor_name=context.executor_name,
        executor_version=context.executor_version,
        tool_name=tool_name,
        request_correlation_id=context.request_correlation_id,
    )
    session.add(attribution)
    if session.get_bind().dialect.name == "postgresql":
        session.connection().execute(
            text("SELECT set_config('finance.execution_attribution_id', :value, true)"),
            {"value": str(attribution.id)},
        )
    session.flush()
    previous = session.info.get(EXECUTION_ATTRIBUTION_SESSION_KEY)
    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution.id
    try:
        yield attribution
        session.flush()
    finally:
        if previous is None:
            session.info.pop(EXECUTION_ATTRIBUTION_SESSION_KEY, None)
        else:
            session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = previous
